"""Small, deterministic RAG store for Ceph runbooks and internal knowledge.

The first implementation intentionally uses an in-process lexical index. It
has no vector database dependency, keeps source citations, filters explicit
Ceph-version metadata, and refuses to index credential-like material.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


class KnowledgeIngestError(ValueError):
    """Raised when a knowledge source contains unsafe or invalid content."""


class KnowledgeVersionMismatch(ValueError):
    """Raised only by strict callers that require a compatible version hit."""


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+./:-]{1,63}", re.IGNORECASE)
_VERSION_RE = re.compile(r"\b(?:ceph\s*)?(\d+)\.(\d+)(?:\.\d+)?\b", re.IGNORECASE)
_CODENAME_RE = re.compile(r"\b(nautilus|octopus|pacific|quincy|reef|squid|tentacle)\b", re.IGNORECASE)
_MAJOR_TO_CODENAME = {
    "14": "nautilus", "15": "octopus", "16": "pacific", "17": "quincy",
    "18": "reef", "19": "squid", "20": "tentacle",
}
INDEX_FORMAT_VERSION = "lexical-v1"
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?key|bot[_ -]?token)\s*[:=]\s*[^\s`]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b"),  # Telegram bot token shape
)


def _fold(text: str) -> str:
    text = str(text or "").replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_fold(text)))


def _major(version: str | None) -> str | None:
    match = _VERSION_RE.search(str(version or ""))
    return match.group(1) if match else None


def _versions(text: str) -> tuple[str, ...]:
    # Numeric strings are common in operational docs (ports, PG counts, dates)
    # and must not silently become a Ceph-version restriction. Numeric version
    # support therefore comes from explicit ``versions=`` metadata; only an
    # unambiguous release codename may be inferred from document text.
    return tuple(sorted({match.group(1).lower() for match in _CODENAME_RE.finditer(text)}))


def _safe_content(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise KnowledgeIngestError("knowledge content is empty")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            raise KnowledgeIngestError("knowledge source contains credential-like material")
    return content.strip()


def _split_sections(content: str, *, max_chars: int) -> list[tuple[str, str]]:
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return [("Overview", content[:max_chars])]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        heading = match.group(2).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end].strip()
        if not body:
            continue
        for offset in range(0, len(body), max_chars):
            part = body[offset:offset + max_chars].strip()
            if part:
                section_name = heading if offset == 0 else f"{heading} (tiếp)"
                sections.append((section_name, part))
    return sections or [("Overview", content[:max_chars])]


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    source: str
    title: str
    section: str
    content: str
    language: str
    versions: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    revision: str = ""
    token_set: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["versions"] = list(self.versions)
        value["components"] = list(self.components)
        value["token_set"] = sorted(self.token_set)
        return value


@dataclass(frozen=True)
class KnowledgeCitation:
    source_id: str
    source: str
    title: str
    section: str
    version: tuple[str, ...]
    confidence: float
    snippet: str
    revision: str
    version_compatible: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["version"] = list(self.version)
        return value


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    hits: tuple[KnowledgeCitation, ...] = ()
    status: str = "no_results"
    version_mismatch: bool = False
    filtered_count: int = 0
    index_revision: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["hits"] = [hit.to_dict() for hit in self.hits]
        value["warnings"] = list(self.warnings)
        return value


def _revision(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


class KnowledgeStore:
    def __init__(self, *, chunk_chars: int = 1600) -> None:
        if chunk_chars < 200:
            raise ValueError("chunk_chars must be at least 200")
        self.chunk_chars = chunk_chars
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._documents: dict[str, str] = {}

    @property
    def index_revision(self) -> str:
        return _revision(*sorted(chunk.chunk_id + chunk.revision for chunk in self._chunks.values()))

    def manifest(self) -> dict[str, Any]:
        """Return a rebuild/audit manifest for the in-process lexical index."""

        return {
            "index_format_version": INDEX_FORMAT_VERSION,
            "index_revision": self.index_revision,
            "chunk_count": len(self._chunks),
            "documents": [
                {"document_id": document_id, "revision": revision}
                for document_id, revision in sorted(self._documents.items())
            ],
        }

    def manifest_is_current(self, manifest: Mapping[str, Any] | None) -> bool:
        """Check whether a previously persisted manifest matches this index."""

        return bool(
            isinstance(manifest, Mapping)
            and manifest.get("index_format_version") == INDEX_FORMAT_VERSION
            and manifest.get("index_revision") == self.index_revision
        )

    def ingest_text(
        self,
        *,
        document_id: str,
        source: str,
        title: str,
        content: str,
        language: str = "vi",
        components: Iterable[str] = (),
        versions: Iterable[str] = (),
    ) -> tuple[KnowledgeChunk, ...]:
        document_id = str(document_id or "").strip()
        source = str(source or "").strip()
        title = str(title or "").strip()
        if not document_id or not source or not title:
            raise KnowledgeIngestError("document_id, source and title are required")
        clean = _safe_content(content)
        declared_versions = tuple(sorted({str(item).strip().lower() for item in versions if str(item).strip()}))
        extracted_versions = _versions(clean)
        all_versions = tuple(sorted(set(declared_versions) | set(extracted_versions)))
        normalized_components = tuple(sorted({str(item).strip().lower() for item in components if str(item).strip()}))
        revision = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
        old_ids = [chunk_id for chunk_id, chunk in self._chunks.items() if chunk.document_id == document_id]
        for chunk_id in old_ids:
            del self._chunks[chunk_id]
        created: list[KnowledgeChunk] = []
        for index, (section, body) in enumerate(_split_sections(clean, max_chars=self.chunk_chars)):
            chunk_id = f"{document_id}:{index}"
            chunk = KnowledgeChunk(
                chunk_id=chunk_id,
                document_id=document_id,
                source=source,
                title=title,
                section=section,
                content=body,
                language=str(language or "unknown").lower(),
                versions=all_versions,
                components=normalized_components,
                revision=revision,
                token_set=frozenset(_tokens(f"{title} {section} {body}")),
            )
            self._chunks[chunk_id] = chunk
            created.append(chunk)
        self._documents[document_id] = revision
        return tuple(created)

    def ingest_file(self, path: str | Path, **metadata: Any) -> tuple[KnowledgeChunk, ...]:
        file_path = Path(path)
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KnowledgeIngestError(f"cannot read knowledge source: {file_path}") from exc
        return self.ingest_text(
            document_id=metadata.pop("document_id", file_path.as_posix()),
            source=metadata.pop("source", file_path.as_posix()),
            title=metadata.pop("title", file_path.stem),
            content=content,
            **metadata,
        )

    def retrieve(
        self,
        query: str,
        *,
        ceph_version: str | None = None,
        component: str | None = None,
        language: str | None = None,
        top_k: int = 5,
        strict_version: bool = True,
    ) -> RetrievalResult:
        query = str(query or "").strip()
        if not query:
            return RetrievalResult(query=query, index_revision=self.index_revision, warnings=("Query rỗng.",))
        if top_k < 1 or top_k > 20:
            raise ValueError("top_k must be between 1 and 20")
        wanted_tokens = _tokens(query)
        wanted_major = _major(ceph_version)
        wanted_version_token = _fold(ceph_version or "")
        component_token = _fold(component or "").strip()
        candidates = list(self._chunks.values())
        filtered_count = 0
        compatible: list[KnowledgeChunk] = []
        for chunk in candidates:
            if language and chunk.language not in {str(language).lower(), "unknown"}:
                filtered_count += 1
                continue
            if component_token and chunk.components and component_token not in chunk.components:
                filtered_count += 1
                continue
            if wanted_major and chunk.versions:
                compatible_version = (
                    wanted_major in chunk.versions
                    or wanted_version_token in chunk.versions
                    or _MAJOR_TO_CODENAME.get(wanted_major, "") in chunk.versions
                )
                if not compatible_version:
                    filtered_count += 1
                    continue
            compatible.append(chunk)
        version_mismatch = bool(wanted_major or wanted_version_token) and not compatible and bool(candidates)
        if version_mismatch and strict_version:
            return RetrievalResult(
                query=query, status="version_mismatch", version_mismatch=True,
                filtered_count=filtered_count, index_revision=self.index_revision,
                warnings=("Không có tài liệu cùng major/codename Ceph; không dùng tài liệu khác version làm căn cứ.",),
            )
        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in compatible:
            overlap = len(wanted_tokens & chunk.token_set)
            if not overlap:
                continue
            exact_code_bonus = 0.35 if any(token.upper().startswith(("OSD_", "PG_", "RGW_", "MON_", "HEALTH_")) and token.upper() in chunk.content.upper() for token in wanted_tokens) else 0.0
            title_bonus = 0.15 if wanted_tokens & _tokens(chunk.title) else 0.0
            score = min(0.99, 0.25 + min(0.55, overlap / max(1, len(wanted_tokens)) * 0.7) + exact_code_bonus + title_bonus)
            scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].source, item[1].chunk_id))
        hits = tuple(
            KnowledgeCitation(
                source_id=f"knowledge:{chunk.chunk_id}", source=chunk.source,
                title=chunk.title, section=chunk.section, version=chunk.versions,
                confidence=round(score, 3), snippet=chunk.content[:800], revision=chunk.revision,
                version_compatible=True,
            )
            for score, chunk in scored[:top_k]
        )
        return RetrievalResult(
            query=query, hits=hits, status="ok" if hits else "no_results",
            version_mismatch=version_mismatch, filtered_count=filtered_count,
            index_revision=self.index_revision,
            warnings=("Kết quả tài liệu không thay thế evidence live từ Ceph.",) if hits else (),
        )


def build_default_knowledge_store(repo_root: str | Path) -> KnowledgeStore:
    """Build a small approved catalog from repository documentation."""

    root = Path(repo_root)
    store = KnowledgeStore()
    catalog = (
        ("docs/ceph-ai-rca-knowledge.md", "Ceph AI RCA knowledge", ("health", "osd", "pg", "rgw")),
        ("docs/runbook-dr.md", "Ceph disaster recovery runbook", ("backup", "restore")),
        ("docs/runbook-log-intelligence.md", "Log Intelligence runbook", ("log", "rca")),
        ("docs/crush-map-monitor.md", "CRUSH map monitoring runbook", ("crush", "osd")),
    )
    for relative, title, components in catalog:
        path = root / relative
        if path.exists():
            store.ingest_file(path, document_id=relative, source=relative, title=title, components=components)
    return store
