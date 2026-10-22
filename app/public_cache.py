"""Short-lived cache of finished storefront API responses, shared correctly across gunicorn workers.

Each storefront route runs several database round trips (about 1-1.5 s in production) while its
answer only changes when the catalog changes. Each worker keeps finished response bodies in its
own memory, and every entry is tagged with the catalog "generation" it was built from.

The generation lives in a small stamp file on the server's local disk, which every worker on the
instance reads (a file read costs microseconds; no database query). Any committed change to a
catalog table rewrites the stamp, so every worker on the instance drops its copies on its very
next request, not just the worker that made the change.

If the stamp can't be rewritten, the worker removes it, and with no stamp no worker serves a
stored entry until a new one is written. The stamp is per instance. If the service ever runs on more than one instance (or briefly during
a zero-downtime deploy, when old and new instances overlap), another instance only notices when
its copy expires, after PUBLIC_CACHE_SECONDS at most. Keep that setting short for that reason.
"""

import hashlib
import itertools
import os
import tempfile
import time

from flask import current_app, has_app_context
from sqlalchemy import event

# Tables whose rows appear in storefront responses. Admin users, shopper data and sessions are not
# here: they never appear in a cached response, so changing them needn't clear anything.
CATALOG_TABLES = frozenset(
    {"products", "product_images", "product_collections", "categories", "collections", "site_settings"}
)
MAX_ENTRIES = 200
_counter = itertools.count()


def default_stamp_path(database_uri):
    """One stamp per database, in the system temp dir that all workers on the instance share."""
    digest = hashlib.sha256(database_uri.encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"snug-public-cache-{digest}")


class PublicCache:
    def __init__(self, ttl, stamp_path, logger, max_entries=MAX_ENTRIES):
        self.ttl = ttl
        self.logger = logger
        self.stamp_path = stamp_path
        self.max_entries = max_entries
        self._entries = {}  # key -> (generation, expires, body)

    @property
    def enabled(self):
        return self.ttl > 0

    def generation(self):
        """The catalog generation all workers agree on: the stamp file's contents, or None when
        there is no usable stamp, in which case nothing is served from or stored in the cache."""
        try:
            with open(self.stamp_path, encoding="ascii") as fh:
                return fh.read() or None
        except FileNotFoundError:
            # First request on this instance, or a worker removed the stamp because it couldn't
            # rewrite it. A brand-new token can't match any stored entry, so this is always safe.
            return self._write_token()
        except OSError:
            return None

    def get(self, key):
        """(body, generation) for a valid entry, or (None, generation) to build it afresh."""
        generation = self.generation()
        hit = self._entries.get(key)
        if generation is not None and hit and hit[0] == generation and hit[1] > time.monotonic():
            return hit[2], generation
        return None, generation

    def put(self, key, generation, body):
        """Store a body built from `generation`, read before the database was queried. If the
        catalog changed while it was being built, the stamp has moved on and it's never served."""
        if generation is None:
            return
        now = time.monotonic()
        if len(self._entries) >= self.max_entries:
            self._entries = {k: v for k, v in self._entries.items() if v[1] > now}
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
        self._entries[key] = (generation, now + self.ttl, body)

    def invalidate(self):
        """Drop this worker's entries and move the stamp on so every other worker drops theirs."""
        self._entries.clear()
        if not self.enabled or self._write_token() is not None:
            return
        # The stamp couldn't be rewritten, so other workers would keep matching the old token.
        # Remove it instead: with no stamp, no worker serves a stored entry (see generation()).
        # Unlinking needs no free space, so it works even when the disk is full.
        try:
            os.remove(self.stamp_path)
        except FileNotFoundError:
            pass
        except OSError:
            # Nothing on disk can be changed: other workers may serve their copies until those
            # expire (PUBLIC_CACHE_SECONDS). Stop caching here and make it loud.
            self.logger.critical(
                "Public cache stamp %s can't be updated or removed; other workers may serve "
                "storefront data up to %s s old. Caching is off in this worker.",
                self.stamp_path,
                self.ttl,
            )
            self.ttl = 0

    def _write_token(self):
        """Atomically write a new, unique token. Returns it, or None if the write failed."""
        token = f"{time.time_ns()}-{os.getpid()}-{next(_counter)}"
        tmp = f"{self.stamp_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="ascii") as fh:
                fh.write(token)
            os.replace(tmp, self.stamp_path)  # atomic: readers see the old token or the new one
            return token
        except OSError:
            self.logger.exception("Couldn't write the public cache stamp at %s", self.stamp_path)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None


def get_cache(app=None):
    return (app or current_app).extensions["snug_public_cache"]


def invalidate():
    if has_app_context() and "snug_public_cache" in current_app.extensions:
        get_cache().invalidate()


def _mark_if_catalog(session, table_name):
    if table_name in CATALOG_TABLES:
        session.info["snug_catalog_changed"] = True


def _after_flush(session, flush_context):
    for obj in (*session.new, *session.dirty, *session.deleted):
        _mark_if_catalog(session, getattr(obj, "__tablename__", None))


def _do_orm_execute(state):
    # Bulk query.update()/delete() skip the flush, so catch them here.
    if (state.is_update or state.is_delete) and state.bind_mapper is not None:
        _mark_if_catalog(state.session, state.bind_mapper.local_table.name)


def _after_commit(session):
    if session.info.pop("snug_catalog_changed", False):
        invalidate()


def _after_rollback(session):
    session.info.pop("snug_catalog_changed", None)


_listening = False


def init_app(app, session):
    global _listening
    stamp = app.config.get("PUBLIC_CACHE_STAMP") or default_stamp_path(app.config["SQLALCHEMY_DATABASE_URI"])
    app.extensions["snug_public_cache"] = PublicCache(app.config["PUBLIC_CACHE_SECONDS"], stamp, app.logger)
    if not _listening:
        # Registered once per process; the handlers act on whichever app is current.
        event.listen(session, "after_flush", _after_flush)
        event.listen(session, "do_orm_execute", _do_orm_execute)
        event.listen(session, "after_commit", _after_commit)
        event.listen(session, "after_rollback", _after_rollback)
        _listening = True
