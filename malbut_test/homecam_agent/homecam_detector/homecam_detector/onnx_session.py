"""Shared provider/thread policy for homecam object and pose inference."""


def create_session(ort, model_path, *, execution_provider, intra_op_num_threads, allow_spinning):
    """Choose a provider explicitly; never hide failed CUDA as CPU inference."""
    if execution_provider not in ('auto', 'cpu', 'cuda'):
        raise ValueError('execution provider must be auto, cpu or cuda')
    if type(intra_op_num_threads) is not int or not 0 <= intra_op_num_threads <= 256:
        raise ValueError('thread count must be an integer in [0, 256]')
    if type(allow_spinning) is not bool:
        raise ValueError('allow_spinning must be bool')
    selected = execution_provider
    if selected == 'auto':
        selected = ('cuda' if 'CUDAExecutionProvider' in ort.get_available_providers() else 'cpu')
    options = {}
    if intra_op_num_threads or not allow_spinning:
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = intra_op_num_threads
        for pool in ('intra_op', 'inter_op'):
            session_options.add_session_config_entry(
                f'session.{pool}.allow_spinning', '1' if allow_spinning else '0')
        options['sess_options'] = session_options
    providers = ['CPUExecutionProvider']
    if selected == 'cuda':
        if 'CUDAExecutionProvider' not in ort.get_available_providers():
            raise RuntimeError('CUDAExecutionProvider is not installed')
        preload = getattr(ort, 'preload_dlls', None)
        if preload is not None:
            preload()
        providers = [('CUDAExecutionProvider', {'use_tf32': 0}), 'CPUExecutionProvider']
    session = ort.InferenceSession(str(model_path), providers=providers, **options)
    if selected == 'cuda':
        if 'CUDAExecutionProvider' not in session.get_providers():
            raise RuntimeError('CUDA initialization failed; refusing CPU-only fallback')
        session.disable_fallback()
    return session, selected
