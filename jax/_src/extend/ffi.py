# Copyright 2024 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import ctypes
import functools
import os
from typing import Any

from jax._src import core
from jax._src import dispatch
from jax._src import dtypes
from jax._src import util
from jax._src.callback import _check_shape_dtype, callback_batching_rule
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src.lib import jaxlib
from jax._src.lib.mlir import ir
from jax._src.typing import Array, ArrayLike, DimSize, DuckTypedArray
import numpy as np

map, unsafe_map = util.safe_map, map


def pycapsule(funcptr):
  """Wrap a ctypes function pointer in a PyCapsule.

  The primary use of this function, and the reason why it lives with in the
  ``jax.extend.ffi`` submodule, is to wrap function calls from external
  compiled libraries to be registered as XLA custom calls.

  Example usage::

    import ctypes
    import jax
    from jax.lib import xla_client

    libfoo = ctypes.cdll.LoadLibrary('./foo.so')
    xla_client.register_custom_call_target(
        name="bar",
        fn=jax.extend.ffi.pycapsule(libfoo.bar),
        platform=PLATFORM,
        api_version=API_VERSION
    )

  Args:
    funcptr: A function pointer loaded from a dynamic library using ``ctypes``.

  Returns:
    An opaque ``PyCapsule`` object wrapping ``funcptr``.
  """
  destructor = ctypes.CFUNCTYPE(None, ctypes.py_object)
  builder = ctypes.pythonapi.PyCapsule_New
  builder.restype = ctypes.py_object
  builder.argtypes = (ctypes.c_void_p, ctypes.c_char_p, destructor)
  return builder(funcptr, None, destructor(0))


def include_dir() -> str:
  """Get the path to the directory containing header files bundled with jaxlib"""
  jaxlib_dir = os.path.dirname(os.path.abspath(jaxlib.__file__))
  return os.path.join(jaxlib_dir, "include")


def ffi_lowering(
    call_target_name: str,
    *,
    operand_layouts: Sequence[Sequence[DimSize]] | None = None,
    result_layouts: Sequence[Sequence[DimSize]] | None = None,
    backend_config: Mapping[str, ir.Attribute] | None = None,
    **lowering_args: Any
) -> mlir.LoweringRule:
  """Build a lowering rule for an foreign function interface (FFI) target.

  By default, this lowering rule can use the input and output abstract values to
  compute the input and output types and shapes for the custom call, assuming
  row-major layouts.

  If keyword arguments are passed to the lowering rule, these are treated as
  attributes, and added to `backend_config`.

  Args:
    call_target_name: The name of the custom call target.
    operand_layouts: A sequence of layouts (dimension orders) for each operand.
      By default, the operands are assumed to be row-major.
    result_layouts: A sequence of layouts (dimension orders) for each result.
      By default, the results are assumed to be row-major.
    backend_config: Configuration data for the custom call. Any keyword
      arguments passed to the lowering rule will added to this dictionary.
    lowering_args: Any other arguments to :func:`mlir.custom_call` will also be
      passed through if provided as extra arguments to this function.
  """

  def _lowering(
    ctx: mlir.LoweringRuleContext, *operands: ir.Value, **params: Any
  ) -> Sequence[ir.Value | Sequence[ir.Value]]:
    kwargs = dict(lowering_args)
    kwargs.setdefault("api_version", 4)
    kwargs["backend_config"] = dict(
      backend_config or {}, **{k: _ir_attribute(v) for k, v in params.items()})
    if "result_types" not in kwargs:
      kwargs["result_types"] = [mlir.aval_to_ir_type(aval) for aval in ctx.avals_out]
    if operand_layouts is None:
      kwargs["operand_layouts"] = _default_layouts(aval.shape for aval in ctx.avals_in)  # pytype: disable=attribute-error
    if result_layouts is None:
      kwargs["result_layouts"] = _default_layouts(aval.shape for aval in ctx.avals_out)

    return mlir.custom_call(call_target_name, operands=operands, **kwargs).results  # type: ignore

  return _lowering


def _default_layouts(shapes: Iterable[Sequence[DimSize]]) -> list[list[DimSize]]:
  return [list(reversed(range(len(shape)))) for shape in shapes]


def _ir_attribute(obj: Any) -> ir.Attribute:
  # TODO(dfm): Similar functions exist in Pallas and Mosaic GPU. Perhaps these
  # could be consolidated into mlir or similar.
  if isinstance(obj, str):
    return ir.StringAttr.get(obj)
  elif isinstance(obj, bool):
    return ir.BoolAttr.get(obj)
  elif isinstance(obj, int):
    return mlir.i64_attr(obj)
  elif isinstance(obj, float):
    return ir.FloatAttr.get_f64(obj)
  elif hasattr(obj, "dtype"):
    if not (dtypes.is_python_scalar(obj) or np.isscalar(obj)):
      raise TypeError("Only scalar attributes are supported")
    mlir_type = mlir.dtype_to_ir_type(obj.dtype)
    if isinstance(mlir_type, ir.IntegerType):
      return ir.IntegerAttr.get(mlir_type, obj)
    elif isinstance(mlir_type, ir.FloatType):
      return ir.FloatAttr.get(mlir_type, obj)
  raise TypeError(f"Unsupported attribute type: {type(obj)}")


ffi_call_p = core.Primitive("ffi_call")
ffi_call_p.multiple_results = True
ffi_call_p.def_impl(functools.partial(dispatch.apply_primitive, ffi_call_p))


@ffi_call_p.def_abstract_eval
def ffi_call_abstract_eval(
    *avals_in,
    result_avals: tuple[core.ShapedArray, ...],
    platforms: tuple[str, ...],
    target_names: tuple[str, ...],
    vectorized: bool,
    **kwargs: Any,
):
  del avals_in, platforms, target_names, vectorized, kwargs
  return result_avals


batching.primitive_batchers[ffi_call_p] = functools.partial(
    callback_batching_rule, ffi_call_p
)


def ffi_call_lowering(
    ctx: mlir.LoweringRuleContext,
    *operands: ir.Value,
    result_avals: tuple[core.ShapedArray, ...],
    platforms: tuple[str, ...],
    target_names: tuple[str, ...],
    vectorized: bool,
    **kwargs: Any,
) -> Sequence[ir.Value]:
  del result_avals, vectorized
  try:
    [platform] = ctx.module_context.platforms
  except ValueError:
    raise ValueError(
        "Multi-plaform lowering is not supported by 'ffi_call'"
    ) from None

  # Here we dispatch based on the target platform. Another option would be to
  # use `lax.platform_dependent` before binding, but that requires that each
  # branch be lowerable on every platform, which isn't the case here.
  try:
    idx = platforms.index(platform)
  except ValueError:
    raise ValueError(
        f"The ffi_call lowering for platform '{platform}' is not defined. "
        f"The available platforms are: {platforms}"
    ) from None

  return ffi_lowering(target_names[idx])(ctx, *operands, **kwargs)


mlir.register_lowering(ffi_call_p, ffi_call_lowering)


def ffi_call(
    platform_target_names: Mapping[str, str],
    result_shape_dtypes: DuckTypedArray | Sequence[DuckTypedArray],
    *args: ArrayLike,
    # TODO(dfm): The `vectorized` parameter is currently ignored. Fix that.
    vectorized: bool = False,
    **kwargs: Any,
) -> Array | Sequence[Array]:
  """Calls a foreign function interface (FFI) target.

  Args:
    platform_target_names: a dictionary where the key is the platform name and
      the value is the name of the XLA FFI custom call target that was
      previously registered for that platform using
      :func:`xla_client.register_custom_call_target`.
    result_shape_dtypes: an object, or sequence of objects, with ``shape`` and
      ``dtype`` attributes which are expected to match the shape and dtype of
      the custom call output or outputs. :class:`jax.ShapeDtypeStruct` is often
      used to define the elements of ``result_shape_dtypes``.
    *args: the arguments passed to the custom call.
    vectorized: boolean specifying whether the callback function can operate in
      a vectorized manner, as described above.
    **kwargs: keyword arguments that are passed as named attributes to the
      custom call using XLA's FFI interface.

  Returns:
    One or more :class:`jax.Array` objects whose shapes and dtypes match
    ``result_shape_dtypes``.
  """
  if "gpu" in platform_target_names:
    raise ValueError("Use 'cuda' or 'rocm' instead of 'gpu' for ffi_call")
  if isinstance(result_shape_dtypes, Sequence):
    multiple_results = True
    result_shape_dtypes_ = result_shape_dtypes
  else:
    multiple_results = False
    result_shape_dtypes_ = (result_shape_dtypes,)
  map(_check_shape_dtype, result_shape_dtypes_)
  result_avals = [
      core.ShapedArray(x.shape, x.dtype) for x in result_shape_dtypes_
  ]
  platforms, target_names = util.unzip2(platform_target_names.items())
  results = ffi_call_p.bind(
      *args,
      result_avals=tuple(result_avals),
      vectorized=vectorized,
      platforms=platforms,
      target_names=target_names,
      **kwargs,
  )
  if multiple_results:
    return results
  else:
    return results[0]
