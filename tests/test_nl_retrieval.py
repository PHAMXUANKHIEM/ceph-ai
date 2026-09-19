from pathlib import Path

import pytest

from shared.natural_language import (
    KnowledgeIngestError,
    KnowledgeStore,
    build_default_knowledge_store,
)


def _store():
    store = KnowledgeStore(chunk_chars=300)
    store.ingest_text(
        document_id="reef-osd",
        source="runbooks/reef-osd.md",
        title="OSD troubleshooting Reef",
        language="vi",
        versions=("reef",),
        components=("osd",),
        content=(
            "# OSD_DOWN\n"
            "Khi OSD down, kiểm tra health detail và OSD tree.\n\n"
            "# Nearfull\n"
            "Kiểm tra pool và phân bố PG trước khi đề xuất xử lý."
        ),
    )
    store.ingest_text(
        document_id="quincy-osd",
        source="runbooks/quincy-osd.md",
        title="OSD troubleshooting Quincy",
        language="vi",
        versions=("quincy",),
        components=("osd",),
        content="# OSD_DOWN\nTài liệu riêng cho Quincy.",
    )
    store.ingest_text(
        document_id="pg-en",
        source="runbooks/pg-en.md",
        title="PG recovery",
        language="en",
        versions=("reef",),
        components=("pg",),
        content="# PG degraded\nCheck PG state and recovery evidence.",
    )
    return store


def test_retrieval_returns_citation_with_source_section_and_revision():
    result = _store().retrieve("OSD down cần kiểm tra gì", ceph_version="18.2.1", component="osd")

    assert result.status == "ok"
    assert result.hits[0].source_id.startswith("knowledge:reef-osd:")
    assert result.hits[0].section == "OSD_DOWN"
    assert result.hits[0].revision
    assert result.hits[0].version_compatible is True


def test_version_mismatch_fails_closed():
    result = _store().retrieve("OSD down", ceph_version="16.2.14", component="osd")

    assert result.status == "version_mismatch"
    assert result.version_mismatch is True
    assert result.hits == ()


def test_index_manifest_has_version_checksum_and_rebuild_signal():
    store = KnowledgeStore()
    store.ingest_text(
        document_id="runbook", source="docs/runbook.md", title="Runbook",
        content="# OSD\nCheck OSD_DOWN.", components=("osd",),
    )

    manifest = store.manifest()

    assert manifest["index_format_version"] == "lexical-v1"
    assert manifest["index_revision"] == store.index_revision
    assert manifest["chunk_count"] == 1
    assert store.manifest_is_current(manifest)

    store.ingest_text(
        document_id="runbook", source="docs/runbook.md", title="Runbook",
        content="# OSD\nCheck OSD_DOWN and OSD_OUT.", components=("osd",),
    )
    assert store.manifest_is_current(manifest) is False


def test_component_and_language_filters_are_applied():
    result = _store().retrieve("PG degraded", ceph_version="reef", component="pg", language="en")

    assert result.status == "ok"
    assert result.hits[0].source == "runbooks/pg-en.md"


def test_ingest_rejects_credential_like_content():
    with pytest.raises(KnowledgeIngestError):
        KnowledgeStore().ingest_text(
            document_id="unsafe", source="unsafe.md", title="Unsafe",
            content="bot_token=12345678:abcdefghijklmnopqrstuvwxyz123456",
        )
    with pytest.raises(KnowledgeIngestError):
        KnowledgeStore().ingest_text(
            document_id="unsafe-key", source="unsafe-key.md", title="Unsafe",
            content="-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        )


def test_upsert_replaces_old_document_revision():
    store = KnowledgeStore()
    store.ingest_text(document_id="doc", source="doc.md", title="Doc", content="# A\nold content")
    first_revision = store.index_revision
    store.ingest_text(document_id="doc", source="doc.md", title="Doc", content="# A\nnew content")

    result = store.retrieve("new content")
    assert result.status == "ok"
    assert result.index_revision != first_revision
    assert "old content" not in result.hits[0].snippet


def test_default_catalog_loads_only_existing_approved_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ceph-ai-rca-knowledge.md").write_text(
        "# Health\nHEALTH_WARN cần xem health detail.", encoding="utf-8"
    )

    store = build_default_knowledge_store(tmp_path)
    result = store.retrieve("HEALTH_WARN")

    assert result.status == "ok"
    assert result.hits[0].source == "docs/ceph-ai-rca-knowledge.md"
