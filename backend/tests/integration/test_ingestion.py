"""Ingestion pipeline: upload, durable jobs, crash recovery, versioning, deletion, PDF."""

from __future__ import annotations

import io
from datetime import timedelta

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select, update

from opsassist.db import global_scope, tenant_scope
from opsassist.jobs import queue
from opsassist.jobs.worker import PermanentJobError
from opsassist.models import Chunk, DocumentVersion, Job, JobStatus, Role, utcnow
from tests.conftest import FailingEmbedder, Harness

RUNBOOK = """# Billing Runbook

Owner: payments team.

## Restart procedure

Run `systemctl restart billing` and verify the queue drains within 5 minutes.

## Escalation

Page the on-call engineer after 15 minutes of failed payments.
"""


def make_pdf(pages: list[str]) -> bytes:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        safe = text.replace("(", "").replace(")", "")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode())
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def _setup(harness: Harness):
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id)
    return owner, client, source


async def _chunks(harness: Harness, workspace_id) -> list[Chunk]:
    async with tenant_scope(harness.container.session_factory, workspace_id) as session:
        return list(await session.scalars(select(Chunk).order_by(Chunk.ordinal)))


async def test_upload_queues_then_worker_indexes_with_metadata(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    uploaded = await harness.upload(
        client, owner.workspace_id, source["id"], "runbook.md", RUNBOOK, ingest=False
    )
    assert uploaded["document"]["latest_status"] == "pending"
    assert uploaded["document"]["current_version_number"] is None
    assert await _chunks(harness, owner.workspace_id) == []

    assert await harness.drain_jobs() == 1

    documents = (
        await client.get(
            f"/api/v1/workspaces/{owner.workspace_id}/sources/{source['id']}/documents"
        )
    ).json()
    assert documents[0]["latest_status"] == "ready"
    assert documents[0]["current_version_number"] == 1
    chunks = await _chunks(harness, owner.workspace_id)
    assert documents[0]["chunk_count"] == len(chunks) == 3
    restart = chunks[1]
    assert restart.section_path == ["Billing Runbook", "Restart procedure"]
    assert RUNBOOK.split("\n")[restart.line_start - 1].startswith("Run `systemctl")
    assert restart.embedding_model == "hash-v1"
    assert restart.content_hash
    assert "Restart procedure" in restart.lexical_text and "runbook" in restart.lexical_text


async def test_identical_upload_is_deduplicated(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    await harness.upload(client, owner.workspace_id, source["id"], "a.md", RUNBOOK)
    second = await harness.upload(client, owner.workspace_id, source["id"], "a.md", RUNBOOK)
    assert second["deduplicated"] is True
    assert second["document"]["latest_version_number"] == 1
    assert await harness.drain_jobs() == 0


async def test_new_version_replaces_old_chunks_atomically(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nOld zebra policy.")
    await harness.upload(
        client, workspace, source["id"], "a.md", "# T\n\nNew giraffe policy.", ingest=False
    )

    # Before the worker runs, search still finds the active version.
    search = f"/api/v1/workspaces/{workspace}/search"
    hits = (await client.post(search, json={"query": "zebra"})).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["version_number"] == 1

    await harness.drain_jobs()
    assert (await client.post(search, json={"query": "zebra"})).json()["hits"] == []
    hits = (await client.post(search, json={"query": "giraffe"})).json()["hits"]
    assert hits[0]["version_number"] == 2
    assert len(await _chunks(harness, workspace)) == 1


async def test_reverting_to_earlier_content_creates_new_active_version(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nZebra.")
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nGiraffe.")
    reverted = await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nZebra.")
    assert reverted["deduplicated"] is False
    hits = (
        await client.post(f"/api/v1/workspaces/{workspace}/search", json={"query": "zebra"})
    ).json()["hits"]
    assert hits[0]["version_number"] == 3


async def test_older_version_finishing_late_does_not_replace_newer(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nZebra.", ingest=False)
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nGiraffe.", ingest=False)
    factory = harness.container.session_factory
    # Run the newer job first, as could happen after a retry of the older one.
    async with global_scope(factory) as session:
        jobs = list(await session.scalars(select(Job).order_by(Job.created_at)))
        await session.execute(
            update(Job)
            .where(Job.id == jobs[0].id)
            .values(run_after=utcnow() + timedelta(milliseconds=500))
        )
    worker = harness.worker()
    assert await worker.run_once()
    import asyncio

    await asyncio.sleep(0.6)
    assert await worker.run_once()
    search = f"/api/v1/workspaces/{workspace}/search"
    assert (await client.post(search, json={"query": "giraffe"})).json()["hits"]
    assert (await client.post(search, json={"query": "zebra"})).json()["hits"] == []


async def test_crashed_worker_job_is_reclaimed_after_lease(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    await harness.upload(client, owner.workspace_id, source["id"], "a.md", RUNBOOK, ingest=False)
    factory = harness.container.session_factory

    # A worker claims the job and dies without any further action.
    crashed = await queue.claim(factory, "crashed-worker", lease_seconds=60)
    assert crashed is not None
    assert await harness.drain_jobs() == 0, "a live lease must not be reclaimed"

    async with global_scope(factory) as session:
        await session.execute(update(Job).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    assert await harness.drain_jobs() == 1
    async with global_scope(factory) as session:
        job = await session.scalar(select(Job))
    assert job.status == JobStatus.SUCCEEDED
    assert job.attempts == 2
    assert len(await _chunks(harness, owner.workspace_id)) == 3


async def test_retry_after_partial_failure_produces_no_duplicates(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    await harness.upload(client, workspace, source["id"], "a.md", RUNBOOK)
    async with tenant_scope(harness.container.session_factory, workspace) as session:
        version = await session.scalar(select(DocumentVersion))
        version.status = "processing"
    async with global_scope(harness.container.session_factory) as session:
        await session.execute(
            update(Job).values(status="queued", attempts=0, run_after=utcnow(), locked_by=None)
        )
    await harness.drain_jobs()
    assert len(await _chunks(harness, workspace)) == 3


async def test_embedding_outage_retries_then_dead_letters_visibly(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    good_embedder = harness.container.embedder
    harness.container.embedder = FailingEmbedder()
    await harness.upload(client, owner.workspace_id, source["id"], "a.md", RUNBOOK)

    async with global_scope(harness.container.session_factory) as session:
        job = await session.scalar(select(Job))
    assert job.status == JobStatus.DEAD
    assert job.attempts == harness.container.settings.job_max_attempts
    assert "ProviderUnavailable" in job.last_error

    documents_url = f"/api/v1/workspaces/{owner.workspace_id}/sources/{source['id']}/documents"
    document = (await client.get(documents_url)).json()[0]
    assert document["latest_status"] == "failed"
    assert document["latest_error"] == (
        "Processing failed after repeated attempts. Upload the file again to retry."
    )
    assert "ProviderUnavailable" not in document["latest_error"], "internals stay on the job"
    assert document["current_version_number"] is None

    # Re-uploading after the outage retries rather than reporting a duplicate.
    harness.container.embedder = good_embedder
    retried = await harness.upload(client, owner.workspace_id, source["id"], "a.md", RUNBOOK)
    assert retried["deduplicated"] is False
    assert (await client.get(documents_url)).json()[0]["latest_status"] == "ready"


async def test_failed_new_version_keeps_previous_version_searchable(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nZebra policy.")
    harness.container.embedder = FailingEmbedder()
    await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nGiraffe policy.")
    hits = (
        await client.post(f"/api/v1/workspaces/{workspace}/search", json={"query": "zebra"})
    ).json()["hits"]
    assert hits[0]["version_number"] == 1


async def test_corrupt_pdf_fails_permanently_without_retries(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    await harness.upload(client, owner.workspace_id, source["id"], "x.pdf", b"%PDF-1.7 garbage")
    async with global_scope(harness.container.session_factory) as session:
        job = await session.scalar(select(Job))
    assert job.status == JobStatus.DEAD
    assert job.attempts == 1
    documents = (
        await client.get(
            f"/api/v1/workspaces/{owner.workspace_id}/sources/{source['id']}/documents"
        )
    ).json()
    assert "could not be parsed" in documents[0]["latest_error"]


async def test_pdf_citations_carry_page_numbers(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    pdf = make_pdf(["Intro page about nothing.", "Refunds require manager approval."])
    await harness.upload(client, owner.workspace_id, source["id"], "policy.pdf", pdf)
    hits = (
        await client.post(
            f"/api/v1/workspaces/{owner.workspace_id}/search", json={"query": "refunds approval"}
        )
    ).json()["hits"]
    assert hits[0]["page"] == 2
    assert "manager approval" in hits[0]["excerpt"]


async def test_scanned_pdf_reports_no_text(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    await harness.upload(client, owner.workspace_id, source["id"], "scan.pdf", buffer.getvalue())
    documents = (
        await client.get(
            f"/api/v1/workspaces/{owner.workspace_id}/sources/{source['id']}/documents"
        )
    ).json()
    assert "no extractable text" in documents[0]["latest_error"]


async def test_delete_document_removes_chunks_blobs_and_results(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    uploaded = await harness.upload(client, workspace, source["id"], "a.md", "# T\n\nZebra.")
    blob_dir = harness.container.settings.blob_dir
    assert list(blob_dir.rglob("*"))
    response = await client.delete(
        f"/api/v1/workspaces/{workspace}/documents/{uploaded['document']['id']}"
    )
    assert response.status_code == 204
    assert await _chunks(harness, workspace) == []
    assert not [path for path in blob_dir.rglob("*") if path.is_file()]
    hits = (
        await client.post(f"/api/v1/workspaces/{workspace}/search", json={"query": "zebra"})
    ).json()["hits"]
    assert hits == []


async def test_document_deleted_while_queued_finishes_cleanly(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    uploaded = await harness.upload(client, workspace, source["id"], "a.md", RUNBOOK, ingest=False)
    await client.delete(f"/api/v1/workspaces/{workspace}/documents/{uploaded['document']['id']}")
    assert await harness.drain_jobs() == 1
    async with global_scope(harness.container.session_factory) as session:
        assert (await session.scalar(select(Job))).status == JobStatus.SUCCEEDED
    assert await _chunks(harness, workspace) == []


async def test_delete_source_cascades(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    workspace = owner.workspace_id
    await harness.upload(client, workspace, source["id"], "a.md", RUNBOOK)
    response = await client.delete(f"/api/v1/workspaces/{workspace}/sources/{source['id']}")
    assert response.status_code == 204
    async with tenant_scope(harness.container.session_factory, workspace) as session:
        assert await session.scalar(select(func.count()).select_from(Chunk)) == 0
        assert await session.scalar(select(func.count()).select_from(DocumentVersion)) == 0


async def test_upload_rejects_oversize_and_bad_type(harness: Harness) -> None:
    owner, client, source = await _setup(harness)
    harness.container.settings.max_upload_bytes = 10
    url = f"/api/v1/workspaces/{owner.workspace_id}/sources/{source['id']}/documents"
    too_big = await client.post(url, files={"file": ("a.md", b"x" * 11)})
    assert too_big.status_code == 413
    bad_type = await client.post(url, files={"file": ("a.exe", b"x")})
    assert bad_type.status_code == 422
    async with global_scope(harness.container.session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_viewer_cannot_upload(harness: Harness) -> None:
    owner, _client, source = await _setup(harness)
    viewer = await harness.member_of(owner.workspace_id, Role.VIEWER)
    viewer_client = await harness.login(viewer.email)
    response = await viewer_client.post(
        f"/api/v1/workspaces/{owner.workspace_id}/sources/{source['id']}/documents",
        files={"file": ("a.md", b"# hi")},
    )
    assert response.status_code == 403


def test_permanent_error_type_is_exported() -> None:
    assert issubclass(PermanentJobError, Exception)
