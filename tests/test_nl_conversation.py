from shared.natural_language import resolve_natural_language_turn


def test_follow_up_inherits_same_cluster_intent_and_resource():
    history = [{"role": "user", "content": "Kiểm tra OSD 12", "cluster_id": "prod"}]

    intent, state = resolve_natural_language_turn("Tại sao?", history, cluster_id="prod")

    assert intent.intent == "osd_health"
    assert intent.resource_ids == ("osd.12",)
    assert intent.decision_reason == "follow_up_context_same_cluster"
    assert state.cluster_id == "prod"
    assert state.resource_ids == ("osd.12",)


def test_context_from_another_cluster_is_not_inherited():
    history = [{"role": "user", "content": "Kiểm tra OSD 12", "cluster_id": "other"}]

    intent, state = resolve_natural_language_turn("Tại sao?", history, cluster_id="prod")

    assert intent.intent == "unknown_or_ambiguous"
    assert intent.needs_clarification is True
    assert state.cluster_id == "prod"
    assert state.resource_ids == ()


def test_unscoped_legacy_history_is_not_inherited():
    history = [{"role": "user", "content": "Kiểm tra OSD 12"}]

    intent, state = resolve_natural_language_turn("Tại sao?", history, cluster_id="prod")

    assert intent.intent == "unknown_or_ambiguous"
    assert state.resource_ids == ()


def test_reset_clears_context_without_changing_server_cluster_scope():
    history = [{"role": "user", "content": "Kiểm tra OSD 12", "cluster_id": "prod"}]

    intent, state = resolve_natural_language_turn("Reset context", history, cluster_id="prod")

    assert intent.intent == "unknown_or_ambiguous"
    assert intent.decision_reason == "conversation_scope_reset"
    assert state.cluster_id == "prod"
    assert state.resource_ids == ()


def test_mutation_follow_up_does_not_inherit_read_only_context():
    history = [{"role": "user", "content": "Kiểm tra OSD 12", "cluster_id": "prod"}]

    intent, state = resolve_natural_language_turn("Xóa nó", history, cluster_id="prod")

    assert intent.intent == "unknown_or_ambiguous"
    assert intent.needs_clarification is True
    assert state.resource_ids == ()
