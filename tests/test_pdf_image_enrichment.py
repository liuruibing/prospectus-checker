from __future__ import annotations

import asyncio
from pathlib import Path

from app.pdf_image_enrichment import enrich_catalogue_reader_context, enrich_package_images


class _FakeVisionProvider:
    cache_namespace = {"provider": "fake", "model": "vision-test"}

    def __init__(self, responses: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.responses = responses or {}
        self.fail = fail
        self.calls: list[dict] = []

    def require_configured(self) -> None:
        return None

    async def generate_multimodal_text(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        *,
        mime_type: str = "image/png",
    ) -> str:
        self.calls.append({"user_prompt": user_prompt, "image_count": len(images), "mime_type": mime_type})
        if self.fail:
            raise RuntimeError("VISION_UPSTREAM_ERROR")
        for key, value in self.responses.items():
            if key in user_prompt:
                return value
        return "| 项目 | 占比 |\n|---|---:|\n| 权益投资 | 99.13% |"


def _sample_package(archive_dir: Path) -> dict:
    return {
        "markdown": "## (二) 投资组合资产配置图表\n\n![原图：物理页 2](assets/p0002-image1.png)\n\n![原图：物理页 2](assets/p0002-image2.png)\n",
        "reader_markdown": "## (二) 投资组合资产配置图表\n\n![原图：物理页 2](assets/p0002-image1.png)\n\n![原图：物理页 2](assets/p0002-image2.png)\n",
        "assets": {
            "assets/p0002-image1.png": b"fake-png-chart-1",
            "assets/p0002-image2.png": b"fake-png-logo-2",
        },
        "archive_path": str(archive_dir),
        "source_map": {
            "original_segments": {
                "s1": {"text": "(二) 投资组合资产配置图表", "page": 2},
                "s2": {"text": "注：以上数据仅供参考", "page": 2},
            },
            "blocks": [
                {"kind": "text", "page": 2, "segments": ["s1"]},
                {"kind": "image", "page": 2, "bbox": [10, 20, 200, 180], "asset": "assets/p0002-image1.png"},
                {"kind": "image", "page": 2, "bbox": [10, 200, 200, 240], "asset": "assets/p0002-image2.png"},
                {"kind": "text", "page": 2, "segments": ["s2"]},
            ],
        },
    }


def test_enrich_package_images_backfills_and_caches(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    pkg = _sample_package(archive_dir)
    provider = _FakeVisionProvider({
        "p0002-image1.png": "```markdown\n数据截止日期：2025-12-31\n\n| 资产类别 | 占比 |\n|---|---:|\n| 权益投资 | 99.13% |\n```",
        "p0002-image2.png": "<!-- decorative-image -->",
    })

    first = asyncio.run(enrich_package_images(pkg, provider=provider))
    assert first["image_ocr_status"] == "COMPLETED"
    assert len(provider.calls) == 2
    assert "(二) 投资组合资产配置图表" in provider.calls[0]["user_prompt"]
    assert "数据截止日期：2025-12-31" in first["reader_markdown"]
    assert "| 权益投资 | 99.13% |" in first["reader_markdown"]
    assert "<!-- image-ocr-start: assets/p0002-image1.png -->" in first["reader_markdown"]
    assert "<!-- image-ocr-start: assets/p0002-image2.png -->" not in first["reader_markdown"]
    assert "数据截止日期：2025-12-31" not in first["raw_reader_markdown"]
    assert (archive_dir / "enriched_reader.md").exists()
    assert (archive_dir / "image_ocr.json").exists()

    second = asyncio.run(enrich_package_images(pkg, provider=provider))
    assert len(provider.calls) == 2  # cache hit, no new model calls
    assert second["reader_markdown"] == first["reader_markdown"]
    assert all(item["cached"] is True for item in second["image_extractions"])


def test_enrich_package_images_degrades_gracefully_on_error(tmp_path: Path) -> None:
    pkg = _sample_package(tmp_path / "archive-fail")
    provider = _FakeVisionProvider(fail=True)

    result = asyncio.run(enrich_package_images(pkg, provider=provider))
    assert result["image_ocr_status"] == "FAILED"
    assert result["reader_markdown"] == pkg["reader_markdown"]
    assert all(item["status"] == "FAILED" for item in result["image_extractions"])


def test_enrich_catalogue_reader_context_updates_units(tmp_path: Path) -> None:
    pkg = _sample_package(tmp_path / "archive-cat")
    provider = _FakeVisionProvider({
        "p0002-image1.png": "| 资产类别 | 占比 |\n|---|---:|\n| 权益投资 | 99.13% |",
        "p0002-image2.png": "<!-- decorative-image -->",
    })
    catalogue = {
        "role": "product_summary",
        "filename": "test.pdf",
        "pages": [
            {
                "page": 2,
                "width": 595.0,
                "height": 842.0,
                "rotation": 0,
                "sources": [
                    {
                        "source_id": "s1",
                        "document_role": "product_summary",
                        "page": 2,
                        "bbox": [10.0, 5.0, 200.0, 18.0],
                        "raw_text": "(二) 投资组合资产配置图表",
                        "section_path": ["二、基金投资与净值表现", "(二) 投资组合资产配置图表"],
                        "section_id": "sec-2",
                    },
                ],
            },
        ],
        "chapters": [
            {
                "section_id": "sec-2",
                "title": "(二) 投资组合资产配置图表",
                "source_ids": ["s1"],
            },
        ],
        "source_count": 1,
        "reader_context": {
            "status": "READY",
            "markdown": pkg["reader_markdown"],
            "logical_units": [
                {"unit_id": "unit-0", "kind": "text", "markdown": "## (二) 投资组合资产配置图表"},
                {"unit_id": "unit-1", "kind": "image", "markdown": "![原图：物理页 2](assets/p0002-image1.png)"},
            ],
        },
    }

    updated = asyncio.run(enrich_catalogue_reader_context(catalogue, pkg, provider=provider))
    rc = updated["reader_context"]
    assert rc["image_ocr_status"] == "COMPLETED"
    assert "| 权益投资 | 99.13% |" in rc["markdown"]
    assert "| 权益投资 | 99.13% |" in rc["logical_units"][1]["markdown"]
    page2_sources = updated["pages"][0]["sources"]
    assert len(page2_sources) == 2
    injected = page2_sources[1]
    assert injected["source_kind"] == "image_ocr"
    assert injected["bbox"] == [10.0, 20.0, 200.0, 180.0]
    assert injected["section_id"] == "sec-2"
    assert "| 权益投资 | 99.13% |" in injected["raw_text"]
    assert updated["source_count"] == 2
    assert injected["source_id"] in updated["chapters"][0]["source_ids"]


def test_inject_image_sources_deduplicates_subcrops_and_suppresses_axis_fragments() -> None:
    from app.pdf_image_enrichment import _inject_image_sources

    catalogue = {
        "role": "product_summary",
        "filename": "summary.pdf",
        "pages": [
            {
                "page": 4,
                "width": 595.0,
                "height": 842.0,
                "rotation": 0,
                "sources": [
                    {
                        "source_id": "hdr",
                        "page": 4,
                        "bbox": [90.0, 50.0, 450.0, 65.0],
                        "raw_text": "(三) 最近十年基金每年的净值增长率及与同期业绩比较基准的比较图",
                        "section_id": "sec-3",
                        "section_path": ["(三) 最近十年"],
                    },
                    {
                        "source_id": "axis_1",
                        "page": 4,
                        "bbox": [143.0, 102.0, 173.0, 112.0],
                        "raw_text": "70.00%",
                        "section_id": "sec-3",
                        "section_path": ["(三) 最近十年"],
                    },
                    {
                        "source_id": "axis_2",
                        "page": 4,
                        "bbox": [189.0, 271.0, 207.0, 280.0],
                        "raw_text": "2016",
                        "section_id": "sec-3",
                        "section_path": ["(三) 最近十年"],
                    },
                    {
                        "source_id": "note_1",
                        "page": 4,
                        "bbox": [90.0, 323.0, 273.0, 335.0],
                        "raw_text": "注：基金的过往业绩不代表未来表现。",
                        "section_id": "sec-3",
                        "section_path": ["(三) 最近十年"],
                    },
                ],
            },
        ],
        "chapters": [
            {
                "section_id": "sec-3",
                "title": "(三) 最近十年",
                "source_ids": ["hdr", "axis_1", "axis_2", "note_1"],
            },
        ],
    }
    extractions = [
        {
            "asset": "assets/p4-full.png",
            "page": 4,
            "bbox": [90.0, 75.0, 505.0, 317.0],
            "markdown": "数据截止日期：2025-12-31\n\n| 年份 | 2016 |\n|---|---|\n| 净值增长率 | 12.71% |",
        },
        {
            "asset": "assets/p4-inner.png",
            "page": 4,
            "bbox": [103.0, 103.0, 349.0, 303.0],
            "markdown": "未识别出数值",
        },
    ]

    pages, chapters, total = _inject_image_sources(catalogue, extractions)
    remaining_texts = [s["raw_text"] for s in pages[0]["sources"]]
    assert total == 3
    assert "70.00%" not in remaining_texts
    assert "2016" not in remaining_texts
    assert "未识别出数值" not in remaining_texts
    assert any("12.71%" in t for t in remaining_texts)
    assert "(三) 最近十年基金每年的净值增长率及与同期业绩比较基准的比较图" in remaining_texts
    assert "注：基金的过往业绩不代表未来表现。" in remaining_texts
    assert "axis_1" not in chapters[0]["source_ids"]
    assert "axis_2" not in chapters[0]["source_ids"]
