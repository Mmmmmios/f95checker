import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import time

from common import parser
from external import error
from indexer import (
    cache,
    f95zone,
)

WATCH_UPDATES_INTERVAL = dt.timedelta(minutes=5).total_seconds()
WATCH_UPDATES_CATEGORIES = f95zone.LATEST_UPDATES_CATEGORIES
WATCH_UPDATES_PAGES = 4
WATCH_VERSIONS_INTERVAL = dt.timedelta(hours=12).total_seconds()
WATCH_VERSIONS_CHUNK_SIZE = 1000

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan():
    updates_task = asyncio.create_task(watch_updates())
    versions_task = asyncio.create_task(watch_versions())

    try:
        yield
    finally:

        updates_task.cancel()
        versions_task.cancel()


# https://stackoverflow.com/a/312464
def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


async def poll_updates():
    try:
        logger.info("Poll updates start")

        invalidate_cache = cache.redis.pipeline()

        for category in WATCH_UPDATES_CATEGORIES:
            for page in range(1, WATCH_UPDATES_PAGES + 1):
                logger.info(f"Poll category {category} page {page}")

                cached_data = cache.redis.pipeline()

                try:
                    async with f95zone.session.get(
                        f95zone.LATEST_UPDATES_URL.format(
                            cmd="list",
                            cat=category,
                            page=page,
                            sort="date",
                            rows=90,
                            ts=int(time.time()),
                        ),
                        cookies=f95zone.cookies,
                    ) as req:
                        res = await req.read()
                except Exception as exc:
                    if index_error := f95zone.check_error(exc, logger):
                        raise Exception(index_error)
                    raise

                if index_error := f95zone.check_error(res, logger):
                    raise Exception(index_error)

                try:
                    updates = json.loads(res)
                except Exception:
                    raise Exception(f"Latest updates returned invalid JSON: {res}")
                if index_error := f95zone.check_error(updates, logger):
                    raise Exception(index_error)

                # We compare version strings to detect updates
                # But also make a hash of other attributes to detect metadata changes
                # We don't save these values directly because we parse from thread content instead
                # But using this meta hash allows to discover metadata changes sooner
                names = []
                current_data = []
                for update in updates["msg"]["data"]:
                    name = cache.NAME_FORMAT.format(id=update["thread_id"])
                    names.append(name)
                    cached_data.hmget(
                        name, "version", cache.HASHED_META, cache.LAST_CACHED
                    )
                    version = str(update["version"])
                    if version == "Unknown":
                        version = None
                    meta = (
                        update["title"],
                        update["creator"],
                        update["prefixes"],
                        update["tags"],
                        round(update["rating"], 1),
                        update["cover"],
                        update["screens"],
                        parser.datestamp(update["ts"]),
                    )
                    meta = hashlib.md5(json.dumps(meta).encode()).hexdigest()
                    current_data.append((version, meta))

                cached_data = await cached_data.execute()

                assert len(names) == len(current_data) == len(cached_data)
                for (
                    name,
                    (version, meta),
                    (cached_version, cached_meta, last_cached),
                ) in zip(names, current_data, cached_data):
                    if cached_version is None or not last_cached:
                        continue

                    version_outdated = version and version != cached_version
                    meta_outdated = meta != cached_meta

                    if version_outdated or meta_outdated:
                        invalidate_cache.hdel(name, cache.LAST_CACHED)
                        invalidate_cache.hset(name, cache.HASHED_META, meta)
                        logger.info(
                            f"Updates: Invalidating cache for {name}"
                            + (
                                f" ({cached_version!r} -> {version!r})"
                                if version_outdated
                                else " (meta changed)"
                            )
                        )

        if len(invalidate_cache):
            result = await invalidate_cache.execute()
            # Skip every 2nd result, those are setting HASHED_META
            invalidated = sum(ret != "0" for ret in result[::2])
            logger.info(f"Updates: Invalidated cache for {invalidated} threads")

        logger.info("Poll updates done")

    except Exception as exc:
        if (
            type(exc) is Exception
            and exc.args
            and type(exc.args[0]) is f95zone.IndexerError
        ):
            index_error = exc.args[0]
        else:
            index_error = f95zone.check_error(exc)

        if index_error:
            pass
            # await asyncio.sleep(
            #     min(index_error.retry_delay, WATCH_UPDATES_INTERVAL)
            # )
            # continue
        else:
            logger.error(f"Error polling updates: {error.text()}\n{error.traceback()}")


async def watch_updates():
    await asyncio.sleep(10)

    while True:
        asyncio.run_coroutine_threadsafe(poll_updates(), asyncio.get_running_loop())
        await asyncio.sleep(WATCH_UPDATES_INTERVAL)


async def poll_versions():
    try:
        logger.info("Poll versions start")

        names = [n async for n in cache.redis.scan_iter("thread:*", 10000, "hash")]
        invalidate_cache = cache.redis.pipeline()

        for names_chunk in chunks(names, WATCH_VERSIONS_CHUNK_SIZE):

            cached_data = cache.redis.pipeline()
            csv = ""
            ids = []
            for name in names_chunk:
                cached_data.hmget(name, "version", cache.LAST_CACHED)
                id = name.split(":")[1]
                csv += f"{id},"
                ids.append(id)
            csv = csv.strip(",")

            try:
                async with f95zone.session.get(
                    f95zone.BULK_VERSION_CHECK_URL.format(threads=csv),
                ) as req:
                    # Await together for efficiency
                    res, cached_data = await asyncio.gather(
                        req.read(), cached_data.execute()
                    )
            except Exception as exc:
                if index_error := f95zone.check_error(exc, logger):
                    raise Exception(index_error)
                raise

            if index_error := f95zone.check_error(res, logger):
                raise Exception(index_error)

            try:
                versions = json.loads(res)
            except Exception:
                raise Exception(f"Versions API returned invalid JSON: {res}")
            if versions.get("msg") in (
                "Missing threads data",
                "Thread not found",
            ):
                versions["status"] = "ok"
                versions["msg"] = {}
            if index_error := f95zone.check_error(versions, logger):
                raise Exception(index_error)
            versions = versions["msg"]

            assert len(names_chunk) == len(ids) == len(cached_data)
            for name, id, (cached_version, last_cached) in zip(
                names_chunk, ids, cached_data
            ):
                if cached_version is None or not last_cached:
                    continue
                version = versions.get(id)
                if not version or version == "Unknown":
                    continue

                if version != cached_version:
                    invalidate_cache.hdel(name, cache.LAST_CACHED)
                    logger.warning(
                        f"Versions: Invalidating cache for {name}"
                        f" ({cached_version!r} -> {version!r})"
                    )

        if len(invalidate_cache):
            result = await invalidate_cache.execute()
            invalidated = sum(ret != "0" for ret in result)
            logger.warning(f"Versions: Invalidated cache for {invalidated} threads")

        logger.info("Poll versions done")

    except Exception as exc:
        if (
            type(exc) is Exception
            and exc.args
            and type(exc.args[0]) is f95zone.IndexerError
        ):
            index_error = exc.args[0]
        else:
            index_error = f95zone.check_error(exc)

        if index_error:
            pass
            # await asyncio.sleep(
            #     min(index_error.retry_delay, WATCH_VERSIONS_INTERVAL)
            # )
            # continue
        else:
            logger.error(f"Error polling versions: {error.text()}\n{error.traceback()}")


async def watch_versions():
    await asyncio.sleep(20)

    while True:
        asyncio.run_coroutine_threadsafe(poll_versions(), asyncio.get_running_loop())
        await asyncio.sleep(WATCH_VERSIONS_INTERVAL)
