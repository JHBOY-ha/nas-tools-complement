"""Immutable subtitle versions and one short, atomic visibility publication."""
import time
import uuid

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.sqlite import insert

from .models import (SUBTITLEPUBLICATION, SUBTITLESTATECLOCK, SUBTITLEAUDITSCOPEHEAD,
                     SUBTITLEAUDITSTATE, SUBTITLEMEDIASTATUS)
from .transactions import DatabaseWriteError, write_transaction

VERSION_MODELS = (SUBTITLEAUDITSTATE, SUBTITLEMEDIASTATUS)
KEYS = {SUBTITLEAUDITSTATE: ('SCOPE_KEY', 'SERVER', 'SUBTITLE_PATH'),
        SUBTITLEMEDIASTATUS: ('SERVER', 'MEDIA_PATH')}


def batch_bytes(rows):
    """Conservative payload/index/WAL budget, computed outside write admission."""
    payload = sum(512 + sum(len(str(value).encode('utf-8')) for value in row.values()) for row in rows)
    return 65536 + payload * 4


def seed_legacy(connection):
    """Seed once without resetting a clock that survives version collection."""
    connection.execute(insert(SUBTITLEPUBLICATION.__table__).values(
        ID='legacy', MODE='baseline', STATUS='published', SEQUENCE=0,
        CREATED_AT=0.0, UPDATED_AT=0.0).on_conflict_do_nothing())
    connection.execute(insert(SUBTITLESTATECLOCK.__table__).values(
        ID=1, SEQUENCE=0).on_conflict_do_nothing())


def latest_criterion(model, deleted=False):
    """Choose latest first, then hide tombstones; never resurrect an older value."""
    table = model.__table__
    publication = SUBTITLEPUBLICATION.__table__.alias('visible_publication')
    newer = table.alias('newer_version')
    newer_publication = SUBTITLEPUBLICATION.__table__.alias('newer_publication')
    sequence = select(publication.c.SEQUENCE).where(
        publication.c.ID == table.c.PUBLICATION_ID,
        publication.c.STATUS == 'published').correlate(table).scalar_subquery()
    fence = 0
    if model is SUBTITLEAUDITSTATE:
        head = SUBTITLEAUDITSCOPEHEAD.__table__
        fence = func.coalesce(select(head.c.REPLACE_SEQUENCE).where(
            head.c.SCOPE_KEY == table.c.SCOPE_KEY,
            head.c.SERVER == table.c.SERVER).correlate(table).scalar_subquery(), 0)
    more_recent = exists(select(1).select_from(newer.join(
        newer_publication, newer_publication.c.ID == newer.c.PUBLICATION_ID)).where(
            *(newer.c[name] == table.c[name] for name in KEYS[model]),
            newer_publication.c.STATUS == 'published',
            newer_publication.c.SEQUENCE > sequence)).correlate(table)
    conditions = [sequence.isnot(None), sequence >= fence, ~more_recent]
    if not deleted:
        conditions.append(table.c.IS_DELETED == 0)
    return and_(*conditions)


def filter_visible(query, objects):
    """Apply to entity and projected-column reads through the DB facade."""
    models = set()
    for obj in objects:
        for model in VERSION_MODELS:
            if obj is model or getattr(obj, 'class_', None) is model:
                models.add(model)
    for model in models:
        query = query.filter(latest_criterion(model))
    return query


def sequence(db):
    return int(db.session.execute(select(SUBTITLESTATECLOCK.SEQUENCE).where(
        SUBTITLESTATECLOCK.ID == 1)).scalar() or 0)


def _deduplicate(rows, model):
    # Preparation is outside SQLite's writer lock. Last duplicate wins, as in
    # the previous UPSERT; expected counts refer to actual logical row keys.
    values = {}
    for row in rows or []:
        value = dict(row)
        values[tuple(value[name] for name in KEYS[model])] = value
    return list(values.values())


def begin(db, audit_rows=(), media_rows=(), task_id=None, server=None,
          scope_key=None, replace=False):
    audit_rows = _deduplicate(audit_rows, SUBTITLEAUDITSTATE)
    media_rows = _deduplicate(media_rows, SUBTITLEMEDIASTATUS)
    publication_id = uuid.uuid4().hex
    now = time.time()
    with write_transaction(db):
        db.session.execute(insert(SUBTITLEPUBLICATION.__table__).values(
            ID=publication_id, TASK_ID=task_id, SERVER=server, SCOPE_KEY=scope_key,
            MODE='replace' if replace else 'upsert', STATUS='building',
            EXPECTED_AUDIT=len(audit_rows), EXPECTED_MEDIA=len(media_rows),
            CREATED_AT=now, UPDATED_AT=now))
    return publication_id, audit_rows, media_rows


def stage(db, publication_id, model, rows):
    """One bounded batch and its receipt commit together; hidden until publish."""
    if not rows:
        return
    pub = SUBTITLEPUBLICATION.__table__
    status = db.session.execute(select(pub.c.STATUS).where(pub.c.ID == publication_id)).scalar()
    if status != 'building':
        raise DatabaseWriteError('审计暂存版本已经中断或发布')
    values = [dict(row, PUBLICATION_ID=publication_id) for row in rows]
    statement = insert(model.__table__)
    statement = statement.on_conflict_do_update(
        index_elements=list(KEYS[model]) + ['PUBLICATION_ID'],
        set_={name: statement.excluded[name] for name in values[0]
              if name not in KEYS[model] and name != 'PUBLICATION_ID'})
    db.session.flush()
    for offset in range(0, len(values), 500):
        db.session.execute(statement, values[offset:offset + 500])
    column = pub.c.WRITTEN_AUDIT if model is SUBTITLEAUDITSTATE else pub.c.WRITTEN_MEDIA
    db.session.execute(pub.update().where(pub.c.ID == publication_id).values(
        {column.name: column + len(values), 'UPDATED_AT': time.time()}))


def publish(db, publication_id):
    """Caller includes the task terminal write in this SAME outer transaction."""
    pub = SUBTITLEPUBLICATION.__table__
    row = db.session.execute(select(pub).where(pub.c.ID == publication_id)).mappings().first()
    if not row:
        raise DatabaseWriteError('审计发布版本不存在')
    if row['STATUS'] == 'published':
        return row['SEQUENCE']
    if row['STATUS'] != 'building' or row['WRITTEN_AUDIT'] != row['EXPECTED_AUDIT'] \
            or row['WRITTEN_MEDIA'] != row['EXPECTED_MEDIA']:
        raise DatabaseWriteError('审计发布批次不完整，旧结果保持可见')
    clock = SUBTITLESTATECLOCK.__table__
    db.session.execute(clock.update().where(clock.c.ID == 1).values(SEQUENCE=clock.c.SEQUENCE + 1))
    current = sequence(db)
    db.session.execute(pub.update().where(pub.c.ID == publication_id).values(
        STATUS='published', SEQUENCE=current, UPDATED_AT=time.time()))
    if row['MODE'] == 'replace':
        head = SUBTITLEAUDITSCOPEHEAD.__table__
        statement = insert(head).values(SCOPE_KEY=row['SCOPE_KEY'], SERVER=row['SERVER'],
                                        REPLACE_SEQUENCE=current)
        db.session.execute(statement.on_conflict_do_update(
            index_elements=['SCOPE_KEY', 'SERVER'], set_={'REPLACE_SEQUENCE': current}))
    return current


def abort(db, publication_id):
    with write_transaction(db):
        pub = SUBTITLEPUBLICATION.__table__
        db.session.execute(pub.update().where(pub.c.ID == publication_id,
                                              pub.c.STATUS == 'building').values(
            STATUS='aborted', UPDATED_AT=time.time()))


def abort_unfinished(db):
    """Recovery never replays a mutation whose last acknowledgement is unknown."""
    with write_transaction(db):
        pub = SUBTITLEPUBLICATION.__table__
        db.session.execute(pub.update().where(pub.c.STATUS == 'building').values(
            STATUS='aborted', UPDATED_AT=time.time()))


def publish_now(db, audit_rows=(), media_rows=(), server=None, scope_key=None, replace=False):
    """Ordinary small updates keep their previous single-transaction semantics."""
    audit_rows = _deduplicate(audit_rows, SUBTITLEAUDITSTATE)
    media_rows = _deduplicate(media_rows, SUBTITLEMEDIASTATUS)
    with write_transaction(db):
        publication_id, audit_rows, media_rows = begin(
            db, audit_rows, media_rows, server=server, scope_key=scope_key, replace=replace)
        stage(db, publication_id, SUBTITLEAUDITSTATE, audit_rows)
        stage(db, publication_id, SUBTITLEMEDIASTATUS, media_rows)
        return publish(db, publication_id)


def delete_rows(db, model, rows):
    rows = list(rows)
    if not rows:
        return 0
    values = []
    for row in rows:
        value = {column.name: getattr(row, column.name) for column in model.__table__.columns
                 if column.name not in ('ID', 'PUBLICATION_ID')}
        value['IS_DELETED'] = 1
        value['UPDATED_AT'] = time.time()
        values.append(value)
    publish_now(db, **({'audit_rows': values} if model is SUBTITLEAUDITSTATE else {'media_rows': values}))
    return len(rows)


def collect(db, batch_rows=1000, max_batches=16):
    """Each cleanup batch re-enters FIFO; latest values and tombstones survive."""
    total = 0
    publication = SUBTITLEPUBLICATION.__table__
    for model in VERSION_MODELS:
        table = model.__table__
        for _ in range(max_batches):
            with write_transaction(db):
                building = exists(select(1).where(publication.c.ID == table.c.PUBLICATION_ID,
                                                  publication.c.STATUS == 'building')).correlate(table)
                ids = db.session.execute(select(table.c.ID).where(
                    ~building, ~latest_criterion(model, deleted=True)).limit(batch_rows)).scalars().all()
                if not ids:
                    break
                for offset in range(0, len(ids), 400):
                    db.session.execute(table.delete().where(table.c.ID.in_(ids[offset:offset + 400])))
                total += len(ids)
        # Once every other physical version of a key is gone, its latest
        # tombstone can also be collected. Never discard it ahead of an older
        # value: that would make a deleted subtitle reappear after cleanup.
        other = table.alias('remaining_version')
        remaining = exists(select(1).where(
            *(other.c[name] == table.c[name] for name in KEYS[model]),
            other.c.ID != table.c.ID)).correlate(table)
        for _ in range(max_batches):
            with write_transaction(db):
                ids = db.session.execute(select(table.c.ID).where(
                    table.c.IS_DELETED == 1, latest_criterion(model, deleted=True),
                    ~remaining).limit(batch_rows)).scalars().all()
                if not ids:
                    break
                for offset in range(0, len(ids), 400):
                    db.session.execute(table.delete().where(table.c.ID.in_(ids[offset:offset + 400])))
                total += len(ids)
    with write_transaction(db):
        referenced = []
        for model in VERSION_MODELS:
            table = model.__table__
            referenced.append(exists(select(1).where(table.c.PUBLICATION_ID == publication.c.ID)))
        ids = db.session.execute(select(publication.c.ID).where(
            publication.c.ID != 'legacy', publication.c.STATUS != 'building',
            *(~value for value in referenced)).limit(batch_rows)).scalars().all()
        for offset in range(0, len(ids), 400):
            db.session.execute(publication.delete().where(publication.c.ID.in_(ids[offset:offset + 400])))
    return total
