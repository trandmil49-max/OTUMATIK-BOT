"""
infrastructure/database/repositories/report_repository.py

Repositories for `reports` and `filter_performance` (SRS Part 15
PROFESSIONAL REPORTING ENGINE + Part 14 FILTER PERFORMANCE).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from core.models import FilterPerformance, Report, ReportType
from infrastructure.database.repositories.base_repository import BaseRepository


class ReportRepository(BaseRepository):
    """Stores generated Daily/Weekly/Monthly/... reports."""

    def create(self, report: Report) -> Report:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reports (
                    report_type, period_start, period_end, generated_at, content,
                    turkish_analysis, turkish_recommendations, overall_grade, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_type.value,
                    self.to_iso(report.period_start),
                    self.to_iso(report.period_end),
                    self.to_iso(report.generated_at),
                    self.to_json(report.content),
                    report.turkish_analysis,
                    report.turkish_recommendations,
                    report.overall_grade,
                    self.to_iso(report.created_at),
                ),
            )
            report.id = cursor.lastrowid
        return report

    def get_latest(self, report_type: ReportType) -> Optional[Report]:
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE report_type = ? ORDER BY period_end DESC LIMIT 1",
                (report_type.value,),
            ).fetchone()
        return self._row_to_report(row) if row else None

    def get_by_period(
        self, report_type: ReportType, period_start_iso: str, period_end_iso: str
    ) -> Optional[Report]:
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE report_type = ? AND period_start = ? AND period_end = ?",
                (report_type.value, period_start_iso, period_end_iso),
            ).fetchone()
        return self._row_to_report(row) if row else None

    def _row_to_report(self, row: sqlite3.Row) -> Report:
        return Report(
            id=row["id"],
            report_type=ReportType(row["report_type"]),
            period_start=self.from_iso(row["period_start"]),
            period_end=self.from_iso(row["period_end"]),
            generated_at=self.from_iso(row["generated_at"]),
            content=self.from_json(row["content"], default={}),
            turkish_analysis=row["turkish_analysis"],
            turkish_recommendations=row["turkish_recommendations"],
            overall_grade=row["overall_grade"],
            created_at=self.from_iso(row["created_at"]),
        )


class FilterPerformanceRepository(BaseRepository):
    """SRS Part 14: 'Never remove filters automatically' -- this table only ever accumulates history."""

    def create(self, performance: FilterPerformance) -> FilterPerformance:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO filter_performance (
                    filter_name, period_start, period_end, trades_rejected, saved_losses_count,
                    contribution_score, success_rate_percent, reliability_score, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    performance.filter_name,
                    self.to_iso(performance.period_start),
                    self.to_iso(performance.period_end),
                    performance.trades_rejected,
                    performance.saved_losses_count,
                    performance.contribution_score,
                    performance.success_rate_percent,
                    performance.reliability_score,
                    self.to_iso(performance.computed_at),
                ),
            )
            performance.id = cursor.lastrowid
        return performance

    def get_history(self, filter_name: str) -> list[FilterPerformance]:
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM filter_performance WHERE filter_name = ? ORDER BY period_start",
                (filter_name,),
            ).fetchall()
        return [self._row_to_performance(row) for row in rows]

    def _row_to_performance(self, row: sqlite3.Row) -> FilterPerformance:
        return FilterPerformance(
            id=row["id"],
            filter_name=row["filter_name"],
            period_start=self.from_iso(row["period_start"]),
            period_end=self.from_iso(row["period_end"]),
            trades_rejected=row["trades_rejected"],
            saved_losses_count=row["saved_losses_count"],
            contribution_score=row["contribution_score"],
            success_rate_percent=row["success_rate_percent"],
            reliability_score=row["reliability_score"],
            computed_at=self.from_iso(row["computed_at"]),
        )
