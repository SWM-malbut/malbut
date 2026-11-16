"""Run a prebuilt TensorRT pose engine without ONNX Runtime.

ONNX Runtime's CUDA provider keeps about 1 GB resident for this model on the
Jetson. Calling TensorRT directly needs neither that runtime nor PyCUDA: the
device buffers come from the CUDA runtime library TensorRT already loads.
"""

import ctypes
from pathlib import Path

import numpy as np


ENGINE_SUFFIX = '.trt-fp16.engine'
_HOST_TO_DEVICE, _DEVICE_TO_HOST = 1, 2


def engine_path_for(model_path) -> Path:
    """Engine built from this ONNX file by build_pose_trt_engine.py."""
    path = Path(model_path)
    return path.with_name(path.stem + ENGINE_SUFFIX)


def _load_cuda_runtime():
    for name in ('libcudart.so.12', 'libcudart.so.11.0', 'libcudart.so'):
        try:
            library = ctypes.CDLL(name)
        except OSError:
            continue
        library.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        library.cudaFree.argtypes = [ctypes.c_void_p]
        library.cudaMemcpy.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        for function in (library.cudaMalloc, library.cudaFree, library.cudaMemcpy):
            function.restype = ctypes.c_int
        return library
    raise RuntimeError('CUDA runtime library (libcudart) is not installed')


class TensorRTSession:
    """One fixed-shape engine with the subset of the ORT session API pose uses."""

    def __init__(self, engine_path, *, trt=None, cuda=None) -> None:
        path = Path(engine_path)
        if not path.is_file():
            raise FileNotFoundError(f'TensorRT pose engine not found: {str(path)!r}')
        if trt is None:
            try:
                import tensorrt as trt
            except ImportError as error:
                raise RuntimeError('TensorRT Python package is not installed') from error
        self._cuda = cuda if cuda is not None else _load_cuda_runtime()
        self._device = {}
        # The runtime must outlive the engine, and the engine its context.
        self._runtime = trt.Runtime(trt.Logger(trt.Logger.ERROR))
        self._engine = self._runtime.deserialize_cuda_engine(path.read_bytes())
        if self._engine is None:
            raise RuntimeError(
                'TensorRT could not load the engine; rebuild it on this robot '
                f'(built for another TensorRT version or GPU?): {path}')
        names = [self._engine.get_tensor_name(index)
                 for index in range(self._engine.num_io_tensors)]
        inputs = [name for name in names
                  if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT]
        outputs = [name for name in names if name not in inputs]
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError('expected one input and one output tensor')
        self.input_name, self._output_name = inputs[0], outputs[0]
        self.input_shape = [int(v) for v in self._engine.get_tensor_shape(self.input_name)]
        self.output_shape = [int(v) for v in self._engine.get_tensor_shape(self._output_name)]
        for name in names:
            if np.dtype(trt.nptype(self._engine.get_tensor_dtype(name))) != np.float32:
                raise ValueError('expected float32 engine tensors')
        if min(self.input_shape + self.output_shape) < 1:
            raise ValueError('expected a fixed-shape engine')
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise RuntimeError('TensorRT could not create an execution context')
        self._output = np.empty(self.output_shape, dtype=np.float32)
        self._input_bytes = int(np.prod(self.input_shape)) * 4
        try:
            self._bindings = [
                self._allocate(name, self._input_bytes if name == self.input_name
                               else self._output.nbytes)
                for name in names]
        except Exception:
            self.close()
            raise

    def _allocate(self, name: str, size: int) -> int:
        pointer = ctypes.c_void_p()
        status = self._cuda.cudaMalloc(ctypes.byref(pointer), size)
        if status != 0 or not pointer.value:
            raise RuntimeError(f'cudaMalloc failed with CUDA error {status}')
        self._device[name] = pointer.value
        return pointer.value

    def _copy(self, destination, source, size: int, kind: int) -> None:
        status = self._cuda.cudaMemcpy(destination, source, size, kind)
        if status != 0:
            raise RuntimeError(f'cudaMemcpy failed with CUDA error {status}')

    def run(self, _outputs, feeds):
        """Infer one blob; mirrors ``InferenceSession.run(None, {name: blob})``."""
        blob = np.ascontiguousarray(feeds[self.input_name], dtype=np.float32)
        if list(blob.shape) != self.input_shape:
            raise ValueError(
                f'pose input shape {list(blob.shape)} does not match engine {self.input_shape}')
        self._copy(self._device[self.input_name], blob.ctypes.data,
                   self._input_bytes, _HOST_TO_DEVICE)
        if not self._context.execute_v2(self._bindings):
            raise RuntimeError('TensorRT inference failed')
        self._copy(self._output.ctypes.data, self._device[self._output_name],
                   self._output.nbytes, _DEVICE_TO_HOST)
        return [self._output.copy()]

    def close(self) -> None:
        """Release device buffers; safe to call twice."""
        device, self._device = getattr(self, '_device', {}), {}
        for pointer in device.values():
            self._cuda.cudaFree(pointer)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
