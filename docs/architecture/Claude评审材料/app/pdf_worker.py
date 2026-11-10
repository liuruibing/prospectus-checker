from __future__ import annotations

import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial

_pool: ProcessPoolExecutor | None = None


async def run_pdf(function, *args):
    """PyMuPDF work must not run concurrently in threads within one process."""
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=max(1, int(os.getenv("PROSPECTUS_PDF_WORKERS", "2"))),
            mp_context=multiprocessing.get_context("spawn"),
        )
    return await asyncio.get_running_loop().run_in_executor(_pool, partial(function, *args))
