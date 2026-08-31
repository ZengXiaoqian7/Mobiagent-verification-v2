from console_compat import configure_utf8_console


class _ReconfigurableStream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_configure_utf8_console_reconfigures_supported_streams():
    stream = _ReconfigurableStream()

    configure_utf8_console((stream, object()))

    assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]
