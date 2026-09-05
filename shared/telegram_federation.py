"""Database scope used by the Telegram gateway.

Each Ceph-AI installation owns one independent cluster and one database.  The
Telegram gateway must therefore stay local to the installation that runs it;
it must never scan or open another server's database.  References retain the
``local:`` qualifier for compatibility with existing callbacks and state.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from config.settings import settings
from shared import db
from shared.clusters import list_active_clusters
from shared.models import Action, ChatMessage, Cluster

logger = logging.getLogger(__name__)

_QUALIFIER_SEPARATOR = ":"
_SOURCE_KEY_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True)
class DatabaseSource:
    key: str
    url: str


@dataclass(frozen=True)
class ClusterTarget:
    source: DatabaseSource
    cluster_id: str
    name: str
    is_default: bool

    @property
    def qualified_id(self) -> str:
        return f"{self.source.key}{_QUALIFIER_SEPARATOR}{self.cluster_id}"


def _source_key(value: str) -> str:
    key = _SOURCE_KEY_RE.sub("-", value.strip().lower()).strip("-")
    if not key:
        raise ValueError("database source key must not be empty")
    return key


def database_sources() -> list[DatabaseSource]:
    """Return only this installation's database.

    Remote database federation was removed because CS-LAB and Hapu-Lab are
    separate deployments.  The old setting is intentionally ignored so a
    stale environment variable cannot make this process read another server.
    """
    return [DatabaseSource("local", settings.database_url)]


def source_for_url(database_url: str | None) -> DatabaseSource | None:
    if not database_url:
        return None
    return next((source for source in database_sources() if source.url == database_url), None)


def qualify_reference(object_id: str) -> str:
    """Qualify a DB object ID for a Telegram callback or persisted state."""
    source = source_for_url(db.current_database_url())
    if source is None:
        return str(object_id)
    return f"{source.key}{_QUALIFIER_SEPARATOR}{object_id}"


def unqualify_reference(reference: str) -> str:
    value = str(reference or "").strip()
    if _QUALIFIER_SEPARATOR not in value:
        return value
    key, object_id = value.split(_QUALIFIER_SEPARATOR, 1)
    if any(source.key == key for source in database_sources()):
        return object_id
    return value


def database_url_for_reference(reference: str, finder) -> str | None:
    """Resolve a qualified reference directly, or scan for legacy IDs."""
    value = str(reference or "").strip()
    if _QUALIFIER_SEPARATOR in value:
        key, object_id = value.split(_QUALIFIER_SEPARATOR, 1)
        source = next((item for item in database_sources() if item.key == key), None)
        if source is not None:
            return source.url
        value = reference
    return finder(value)


def _load_active_with_models(source: DatabaseSource) -> list[tuple[ClusterTarget, Cluster]]:
    try:
        with db.use_database(source.url):
            with db.SessionLocal() as session:
                clusters = list(list_active_clusters(session))
                result = []
                for cluster in clusters:
                    target = ClusterTarget(
                        source, str(cluster.id), str(cluster.name), bool(cluster.is_default)
                    )
                    session.expunge(cluster)
                    result.append((target, cluster))
                return result
    except Exception:
        logger.exception("telegram federation: cannot read source %s", source.key)
        return []


def active_clusters_with_models() -> list[tuple[ClusterTarget, Cluster]]:
    """Return active clusters plus detached ORM models for the caller's DB."""
    items = [
        item
        for source in database_sources()
        for item in _load_active_with_models(source)
    ]
    items.sort(key=lambda item: (not item[0].is_default, item[0].name.casefold(), item[0].source.key))
    return items


def active_clusters() -> list[ClusterTarget]:
    """Return active clusters from every reachable configured database."""
    return [target for target, _cluster in active_clusters_with_models()]


def _find_target(qualified_id: str, *, active_only: bool = True) -> ClusterTarget | None:
    selected = str(qualified_id or "").strip()
    if not selected:
        return None
    parts = selected.split(_QUALIFIER_SEPARATOR, 1)
    source_key = parts[0] if len(parts) == 2 else None
    raw_cluster_id = parts[1] if len(parts) == 2 else selected
    matches = []
    for source in database_sources():
        if source_key is not None and source.key != source_key:
            continue
        for target in (
            [target for target, _cluster in _load_active_with_models(source)]
            if active_only
            else _load_all(source)
        ):
            if target.cluster_id == raw_cluster_id:
                matches.append(target)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning("telegram federation: ambiguous cluster id %s", selected)
    return None


def _load_all(source: DatabaseSource) -> list[ClusterTarget]:
    try:
        with db.use_database(source.url):
            with db.SessionLocal() as session:
                return [
                    ClusterTarget(source, str(cluster.id), str(cluster.name), bool(cluster.is_default))
                    for cluster in session.query(Cluster).all()
                ]
    except Exception:
        logger.exception("telegram federation: cannot read source %s", source.key)
        return []


def resolve_cluster(qualified_id: str) -> tuple[ClusterTarget, Cluster] | None:
    """Resolve a selected cluster and detach its ORM object from its DB."""
    target = _find_target(qualified_id)
    if target is None:
        return None
    with db.use_database(target.source.url):
        with db.SessionLocal() as session:
            cluster = session.get(Cluster, target.cluster_id)
            if cluster is None or not cluster.is_active:
                return None
            session.expunge(cluster)
            return target, cluster


def target_for_actor_cluster(cluster_id: str) -> tuple[ClusterTarget, Cluster] | None:
    """Resolve qualified or legacy unqualified persisted Telegram choices."""
    return resolve_cluster(cluster_id)


def database_urls_for_message(message_id: str) -> list[str]:
    urls = []
    for source in database_sources():
        try:
            with db.use_database(source.url):
                with db.SessionLocal() as session:
                    if session.get(ChatMessage, str(message_id)) is not None:
                        urls.append(source.url)
        except Exception:
            logger.exception("telegram federation: cannot search messages in %s", source.key)
    return urls


def database_url_for_message(message_id: str) -> str | None:
    urls = database_urls_for_message(message_id)
    if len(urls) == 1:
        return urls[0]
    if len(urls) > 1:
        logger.warning("telegram federation: ambiguous message id %s", message_id)
    return None


def database_urls_for_message_reference(reference: str) -> list[str]:
    value = str(reference or "").strip()
    if _QUALIFIER_SEPARATOR in value:
        key, _object_id = value.split(_QUALIFIER_SEPARATOR, 1)
        source = next((item for item in database_sources() if item.key == key), None)
        if source is not None:
            return [source.url]
    return database_urls_for_message(value)


def database_url_for_message_reference(reference: str) -> str | None:
    urls = database_urls_for_message_reference(reference)
    return urls[0] if len(urls) == 1 else None


def database_urls_for_action(action_id: str) -> list[str]:
    urls = []
    for source in database_sources():
        try:
            with db.use_database(source.url):
                with db.SessionLocal() as session:
                    if session.get(Action, str(action_id)) is not None:
                        urls.append(source.url)
        except Exception:
            logger.exception("telegram federation: cannot search actions in %s", source.key)
    return urls


def database_url_for_action(action_id: str) -> str | None:
    urls = database_urls_for_action(action_id)
    if len(urls) == 1:
        return urls[0]
    if len(urls) > 1:
        logger.warning("telegram federation: ambiguous action id %s", action_id)
    return None


def database_urls_for_action_reference(reference: str) -> list[str]:
    value = str(reference or "").strip()
    if _QUALIFIER_SEPARATOR in value:
        key, _object_id = value.split(_QUALIFIER_SEPARATOR, 1)
        source = next((item for item in database_sources() if item.key == key), None)
        if source is not None:
            return [source.url]
    return database_urls_for_action(value)


def database_url_for_action_reference(reference: str) -> str | None:
    urls = database_urls_for_action_reference(reference)
    return urls[0] if len(urls) == 1 else None
