from llm import call_llm


class FakeMessages:
    def create(self, **kwargs):
        class R:
            content = [type("Block", (), {"text": "hello from stub"})()]
        return R()


class FakeClient:
    messages = FakeMessages()


def test_call_llm_returns_text_from_client():
    out = call_llm("say hi", system="be terse", client=FakeClient())
    assert out == "hello from stub"
