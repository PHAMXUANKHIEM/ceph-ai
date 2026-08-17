"""Periodic compatibility Bucket Logging delivery for Ceph 14-19."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import BucketLoggingConfig, Cluster
from watcher.rgw_access_log import (
    create_s3_access_key, create_s3_access_key_with, fetch_bucket_access_log,
    fetch_bucket_access_log_with, revoke_s3_access_key, revoke_s3_access_key_with,
)

logger = logging.getLogger(__name__)


def _deliver(config: BucketLoggingConfig, cluster: Cluster) -> None:
    nodes = configured_nodes() if cluster.is_default else configured_nodes(cluster)
    hosts = [str(node["host"]) for node in nodes if "RGW" in node["roles"]]
    records = []
    for host in hosts:
        if cluster.is_default:
            records.extend(fetch_bucket_access_log(host, config.source_bucket))
        else:
            ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
            records.extend(fetch_bucket_access_log_with(
                host, config.source_bucket, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name))
    records.sort(key=lambda row: (row.get("timestamp_raw") or "", row.get("method") or "", row.get("path") or ""))
    fresh = []
    checkpoint = config.checkpoint or ""
    for row in records:
        key = "|".join((str(row.get("timestamp_raw") or ""), str(row.get("method") or ""), str(row.get("path") or "")))
        if key > checkpoint:
            fresh.append((key, row))
    if not fresh:
        return
    host = hosts[0]
    if cluster.is_default:
        credential = create_s3_access_key(host, config.owner)
    else:
        ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
        credential = create_s3_access_key_with(host, config.owner, ssh_user, ssh_key, mode,
                                                cluster.ceph_rgw_container_name)
    access_key = credential["access_key"]
    try:
        client = boto3.client("s3", endpoint_url=config.endpoint, aws_access_key_id=access_key,
            aws_secret_access_key=credential["secret_key"],
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        now = datetime.now(timezone.utc)
        object_key = f"{config.prefix}{now:%Y/%m/%d/%Y-%m-%d-%H-%M-%S-%f}.jsonl"
        body = "\n".join(json.dumps({k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in row.items()}, ensure_ascii=False, separators=(",", ":")) for _key, row in fresh) + "\n"
        client.put_object(Bucket=config.target_bucket, Key=object_key, Body=body.encode(),
                          ContentType="application/x-ndjson")
    finally:
        if cluster.is_default:
            revoke_s3_access_key(host, config.owner, access_key)
        else:
            revoke_s3_access_key_with(host, config.owner, access_key, ssh_user, ssh_key, mode,
                                      cluster.ceph_rgw_container_name)
    config.checkpoint = fresh[-1][0]
    config.last_delivery_at = datetime.utcnow()
    config.last_error = None


def collect_once() -> None:
    with db.SessionLocal() as session:
        rows = session.query(BucketLoggingConfig).filter_by(enabled=True, mode="compatibility").all()
        for config in rows:
            cluster = session.get(Cluster, config.cluster_id)
            if not cluster or not cluster.is_active:
                continue
            try:
                _deliver(config, cluster)
            except Exception as exc:
                logger.exception("compatibility bucket logging failed for %s", config.source_bucket)
                config.last_error = str(exc)[:1000]
            config.updated_at = datetime.utcnow()
        session.commit()


async def run(interval_seconds: int = 300) -> None:
    while True:
        await asyncio.to_thread(collect_once)
        await asyncio.sleep(interval_seconds)
