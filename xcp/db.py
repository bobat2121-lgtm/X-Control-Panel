"""Storage. SQLite locally, Postgres in the cloud (set DATABASE_URL)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    delete,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from xcp.config import DATA_DIR, env
from xcp.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Item(Base):
    """Anything scanned: an X post, news article, SEC filing."""
    __tablename__ = "items"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)  # x:<id> | rss:<hash> | edgar:<acc>
    kind: Mapped[str] = mapped_column(String(20), index=True)  # x_post | news | filing
    source: Mapped[str] = mapped_column(String(100), default="")  # watchlist | search:<name> | feed name
    author: Mapped[str] = mapped_column(String(120), default="")
    author_name: Mapped[str] = mapped_column(String(200), default="")
    author_followers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    lane: Mapped[str] = mapped_column(String(10), default="btc", index=True)
    pillar: Mapped[str] = mapped_column(String(30), default="bitcoin")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Story(Base):
    __tablename__ = "stories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slot: Mapped[str] = mapped_column(String(30), default="")
    slot_date: Mapped[str] = mapped_column(String(10), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    key: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    lane: Mapped[str] = mapped_column(String(10), default="btc")
    pillar: Mapped[str] = mapped_column(String(30), default="bitcoin")
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    item_ids: Mapped[list] = mapped_column(JSON, default=list)
    angle_ideas: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new | starred | used | dismissed


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    slot: Mapped[str] = mapped_column(String(30), index=True)  # premarket | ai_noon | midday | friday_close | on_demand
    slot_date: Mapped[str] = mapped_column(String(10), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="regular")  # regular | showcase | reply
    story_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lane: Mapped[str] = mapped_column(String(10), default="btc")
    pillar: Mapped[str] = mapped_column(String(30), default="bitcoin")
    tone: Mapped[str] = mapped_column(String(20), default="analytical")
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # new | edited | posted | dismissed | banked | snoozed
    score: Mapped[float] = mapped_column(Float, default=0.0)
    title: Mapped[str] = mapped_column(Text, default="")
    inspiration: Mapped[list] = mapped_column(JSON, default=list)  # [{label,url,author,text,metrics}]
    numbers: Mapped[list] = mapped_column(JSON, default=list)  # [{label,value,source}]
    editor_flags: Mapped[list] = mapped_column(JSON, default=list)
    chart_hint: Mapped[str] = mapped_column(String(50), default="none")
    build_idea_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    posted_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chosen_label: Mapped[str | None] = mapped_column(String(5), nullable=True)
    dismiss_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Variant(Base):
    __tablename__ = "variants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str] = mapped_column(String(5))  # A | B | C
    style: Mapped[str] = mapped_column(String(20), default="analyst")
    parts: Mapped[list] = mapped_column(JSON, default=list)  # 1 part = single post, >1 = thread
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(80), default="ai")  # ai | me | ai:<instruction> | tool:<name>
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Post(Base):
    """Your actual posts on X (imported nightly or logged from the Feed)."""
    __tablename__ = "posts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    url: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lane: Mapped[str] = mapped_column(String(10), default="btc")
    pillar: Mapped[str] = mapped_column(String(30), default="bitcoin")
    tone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    style: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="regular")
    source: Mapped[str] = mapped_column(String(20), default="panel")  # panel | x_api
    latest_metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class PostMetric(Base):
    __tablename__ = "post_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String(40), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    bookmarks: Mapped[int] = mapped_column(Integer, default=0)


class BuildIdea(Base):
    __tablename__ = "build_ideas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    title: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    format: Mapped[str] = mapped_column(String(40), default="streamlit_page")
    lane: Mapped[str] = mapped_column(String(10), default="btc")
    pillar: Mapped[str] = mapped_column(String(30), default="digital_credit")
    why_now: Mapped[str] = mapped_column(Text, default="")
    signal_links: Mapped[list] = mapped_column(JSON, default=list)
    concept: Mapped[str] = mapped_column(Text, default="")
    data_inputs: Mapped[str] = mapped_column(Text, default="")
    build_spec: Mapped[str] = mapped_column(Text, default="")
    effort: Mapped[str] = mapped_column(String(2), default="M")  # S | M | L
    impact: Mapped[int] = mapped_column(Integer, default=5)
    novelty: Mapped[int] = mapped_column(Integer, default=5)
    timeliness: Mapped[int] = mapped_column(Integer, default=5)
    evergreen: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    launch_post: Mapped[str] = mapped_column(Text, default="")
    followups: Mapped[list] = mapped_column(JSON, default=list)
    build_prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="inbox", index=True)
    # inbox | shortlist | building | ready | shipped | archived
    series: Mapped[str] = mapped_column(String(120), default="")
    showcase_date: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    showcase_slot: Mapped[str | None] = mapped_column(String(30), nullable=True)
    shipped_url: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="ai")


class Request(Base):
    """On-demand agent work queued from the panel (rewrites, draft-from-link, ...)."""
    __tablename__ = "requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(40), index=True)
    run_date: Mapped[str] = mapped_column(String(10), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | error
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[str] = mapped_column(Text, default="")
    x_reads: Mapped[int] = mapped_column(Integer, default=0)


class StyleExample(Base):
    """Voice & style library. 'mine' = your own best posts (verbatim, highest priority).
    'admired' = craft patterns learned from accounts you admire: pattern, skeleton and an original
    demo in your lane, plus a link. Their wording is never stored or reused."""
    __tablename__ = "style_examples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(10), default="admired", index=True)  # admired | mine
    handle: Mapped[str] = mapped_column(String(80), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    format: Mapped[str] = mapped_column(String(30), default="short_observation", index=True)
    # short_observation | quick_analysis | long_analysis | thread | humor_meme | contrarian | data_callout
    # | news_reaction | question_hook | chart_callout
    length: Mapped[str] = mapped_column(String(10), default="short")  # short | medium | long
    pillar: Mapped[str] = mapped_column(String(30), default="bitcoin")
    hook_type: Mapped[str] = mapped_column(String(60), default="")
    pattern: Mapped[str] = mapped_column(Text, default="")  # how the post is built
    skeleton: Mapped[str] = mapped_column(Text, default="")  # fill-in-the-blanks template
    demo: Mapped[str] = mapped_column(Text, default="")  # original example in your lane
    why_it_works: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")  # verbatim ONLY for source == 'mine'
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    strength: Mapped[int] = mapped_column(Integer, default=7)  # 1-10
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ShowcaseRun(Base):
    """One Digital Credit Report panel for one showcase day: every audit attempt, then the image you post."""
    __tablename__ = "showcase_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[str] = mapped_column(String(10), index=True)  # the New York showcase date
    panel: Mapped[str] = mapped_column(String(20), index=True)  # monday | wednesday | friday
    slot: Mapped[str] = mapped_column(String(30), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    # waiting | ready | blocked | missed | posted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # readiness checks
    renders: Mapped[int] = mapped_column(Integer, default=0)  # full render + audit passes
    de_commit: Mapped[str] = mapped_column(String(40), default="")  # digital-exposure code that drew it
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False)  # last-good code, main was broken
    checks: Mapped[list] = mapped_column(JSON, default=list)  # our gate: [{id, status, detail}]
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    audit_summary: Mapped[dict] = mapped_column(JSON, default=dict)  # digital-exposure PASS/WARN/FAIL counts
    audit: Mapped[dict] = mapped_column(JSON, default=dict)  # the panel's displayed values + notes
    audit_checks: Mapped[list] = mapped_column(JSON, default=list)  # digital-exposure checks for this panel
    filings: Mapped[dict] = mapped_column(JSON, default=dict)  # Monday: the 8-Ks it was checked against
    png: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # latest render (the post image once ready)
    png_sha256: Mapped[str] = mapped_column(String(64), default="")
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alerts: Mapped[dict] = mapped_column(JSON, default=dict)  # which Discord pings were sent
    log: Mapped[str] = mapped_column(Text, default="")


class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    time: Mapped[str] = mapped_column(String(5), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    pillar: Mapped[str] = mapped_column(String(30), default="macro")
    importance: Mapped[int] = mapped_column(Integer, default=2)  # 1 low .. 3 high
    notes: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- engine

_engine = None


def db_url() -> str:
    url = env("DATABASE_URL")
    if not url:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def engine():
    global _engine
    if _engine is None:
        url = db_url()
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)
        Base.metadata.create_all(_engine)
    return _engine


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


def is_postgres() -> bool:
    return db_url().startswith("postgresql")


# ---------------------------------------------------------------- kv helpers

def kv_get(key: str, default=None):
    with session() as s:
        row = s.get(KV, key)
        return row.value if row is not None else default


def kv_set(key: str, value) -> None:
    with session() as s:
        row = s.get(KV, key)
        if row is None:
            s.add(KV(key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()
        s.commit()


def kv_delete(key: str) -> None:
    with session() as s:
        s.execute(delete(KV).where(KV.key == key))
        s.commit()


# ---------------------------------------------------------------- common queries

def latest_snapshot() -> Snapshot | None:
    with session() as s:
        return s.scalars(select(Snapshot).order_by(Snapshot.taken_at.desc()).limit(1)).first()


def current_variants(s: Session, draft_id: int) -> list[Variant]:
    rows = s.scalars(
        select(Variant).where(Variant.draft_id == draft_id, Variant.is_current.is_(True)).order_by(Variant.label)
    ).all()
    return list(rows)


def add_variant_version(s: Session, draft_id: int, label: str, parts: list[str], style: str, created_by: str) -> Variant:
    """Append a new version for a draft option; older versions stay for undo."""
    prev = s.scalars(
        select(Variant).where(Variant.draft_id == draft_id, Variant.label == label).order_by(Variant.version.desc())
    ).first()
    for old in s.scalars(select(Variant).where(Variant.draft_id == draft_id, Variant.label == label, Variant.is_current.is_(True))):
        old.is_current = False
    v = Variant(
        draft_id=draft_id,
        label=label,
        style=style or (prev.style if prev else "analyst"),
        parts=[p for p in parts if p.strip()] or [""],
        version=(prev.version + 1) if prev else 1,
        created_by=created_by,
        is_current=True,
    )
    s.add(v)
    return v


def pending_requests(s: Session, draft_id: int | None = None) -> list[Request]:
    q = select(Request).where(Request.status.in_(["pending", "running"]))
    rows = s.scalars(q.order_by(Request.created_at)).all()
    if draft_id is not None:
        rows = [r for r in rows if (r.payload or {}).get("draft_id") == draft_id]
    return list(rows)
