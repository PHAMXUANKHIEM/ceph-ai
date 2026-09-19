from shared.natural_language import route_natural_language


def test_routes_vietnamese_cluster_health_without_side_effects():
    result = route_natural_language("Cụm đang HEALTH_WARN vì sao?", cluster_id="cs-lab")

    assert result.intent == "cluster_health"
    assert result.cluster_id == "cs-lab"
    assert result.mode == "read_only"
    assert result.needs_clarification is False


def test_extracts_osd_id_threshold_and_time_range():
    result = route_natural_language(
        "Kiểm tra OSD 12 có utilization trên 80% trong 15 phút gần đây",
        cluster_id="prod",
    )

    assert result.intent == "osd_health"
    assert result.resource_type == "osd"
    assert result.resource_ids == ("osd.12",)
    assert result.filters == {"utilization_gte": 80}
    assert result.time_range.duration_seconds == 15 * 60


def test_extracts_pg_and_node_entities():
    pg = route_natural_language("PG 1.a bị degraded", cluster_id="prod")
    node = route_natural_language("Kiểm tra node 10.3.55.213", cluster_id="prod")

    assert pg.intent == "pg_health"
    assert pg.resource_type == "pg"
    assert pg.resource_ids == ("1.a",)
    assert node.intent == "node_metrics"
    assert node.resource_ids == ("10.3.55.213",)


def test_mutation_like_request_is_never_routed_to_an_executor():
    result = route_natural_language("Xóa volume test-rbd", cluster_id="prod")

    assert result.intent == "unknown_or_ambiguous"
    assert result.mode == "read_only"
    assert result.needs_clarification is True
    assert "read-only" in (result.clarification_question or "")


def test_unknown_request_has_clarification_question():
    result = route_natural_language("Giúp tôi với", cluster_id="prod")

    assert result.intent == "unknown_or_ambiguous"
    assert result.needs_clarification is True
    assert result.to_dict()["resource_ids"] == []


def test_time_range_supports_hours_and_yesterday():
    assert route_natural_language("xem log trong 2 giờ").time_range.duration_seconds == 7200
    assert route_natural_language("xem log hôm qua").time_range.duration_seconds == 86400

