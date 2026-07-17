from __future__ import annotations

TABLE_REVIEW_COMPLETE_STATUSES = ("complete", "completed", "reviewed", "accepted", "confirmed")

TABLE_REVIEW_ATTENTION_STATUSES = ("needs_fix", "fix_required", "rejected", "blocked", "not_matched")

TABLE_REVIEW_ALLOWED_UNIT_STATUSES = TABLE_REVIEW_COMPLETE_STATUSES + TABLE_REVIEW_ATTENTION_STATUSES

TABLE_REVIEW_TRUE_VALUES = ("1", "true", "yes", "y", "ok", "pass", "passed", "confirmed")

TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS = (
    "human_source_pages_checked",
    "human_unit_status",
    "human_manual_table_count",
    "human_matched_table_count",
    "human_row_column_match",
    "human_parentage_ok",
    "human_reviewer",
    "human_reviewed_at",
)

TABLE_REVIEW_COMPLETION_GUIDANCE = (
    "Use a complete status only after checking the original source pages, entering manual and matched "
    "table counts, confirming row/column shape and parentage, and adding reviewer/date audit fields."
)
