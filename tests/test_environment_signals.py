from app_test_agent.environment_signals import detect_environment_blocker


def test_unrelated_restricted_login_message_is_not_global_environment_blocker():
    assert detect_environment_blocker(["对方账号异常，已被限制登录，消息无法送达"]) is None


def test_explicit_login_prompt_and_button_remain_environment_blockers():
    assert detect_environment_blocker(["请先登录后继续"]) == "请先登录"
    assert detect_environment_blocker(["登录"]) == "登录"
