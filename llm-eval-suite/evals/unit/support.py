"""What both unit test files share: example tables, SQL, a judge that must
never be called, and a test case for the SQL metrics."""

from typing import Tuple

from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase

from metrics.common import NO_OUTPUT
from suite.schema_metadata import IndexHint, TableMetadata

# Made-up tables for the unit tests, so they don't change when
# schema_metadata.json does. The `metadata` fixture (unit/conftest.py) gives
# them keyed by lowercase name.
EXAMPLE_TABLES: Tuple[TableMetadata, ...] = (
    TableMetadata(
        name="YOUR_FACT_TABLE",
        description="Raw event-level facts, one row per transaction.",
        approx_row_count=500_000_000,
        columns={
            "event_id": "bigint",
            "event_ts": "timestamp",
            "event_date": "date",
            "customer_id": "bigint",
            "product_id": "bigint",
            "revenue": "numeric(12,2)",
            "quantity": "integer",
        },
        partition_column="event_date",
        indexes=(
            IndexHint("YOUR_FACT_TABLE_pkey", ("event_id",)),
            IndexHint("ix_fact_customer_date", ("customer_id", "event_date")),
            IndexHint("brin_fact_event_ts", ("event_ts",), kind="brin"),
        ),
        preferred_for=("individual transactions", "per-customer detail"),
    ),
    TableMetadata(
        name="YOUR_DAILY_ROLLUP_TABLE",
        description="Revenue and quantity aggregated per day and product.",
        approx_row_count=2_000_000,
        columns={
            "event_date": "date",
            "product_id": "bigint",
            "total_revenue": "numeric(14,2)",
            "total_quantity": "bigint",
            "order_count": "bigint",
        },
        indexes=(
            IndexHint(
                "YOUR_DAILY_ROLLUP_TABLE_pkey", ("event_date", "product_id")
            ),
        ),
        rollup_of="YOUR_FACT_TABLE",
        preferred_for=("daily", "weekly", "monthly", "trend", "total revenue"),
    ),
    TableMetadata(
        name="YOUR_DIMENSION_TABLE",
        description="Customer dimension, one row per customer.",
        approx_row_count=1_000_000,
        columns={
            "customer_id": "bigint",
            "region": "text",
            "segment": "text",
            "signup_date": "date",
            "email": "text",
            "phone": "text",
        },
        indexes=(
            IndexHint("YOUR_DIMENSION_TABLE_pkey", ("customer_id",)),
            IndexHint("ix_dim_region", ("region",)),
        ),
        preferred_for=("region", "segment", "customer attributes"),
        sensitive_columns=("email", "phone"),
    ),
)

DAILY_REVENUE_PROMPT = "What was total revenue per day over the last 30 days?"
ROLLUP_SQL = """
    SELECT event_date, SUM(total_revenue) AS revenue
    FROM public.YOUR_DAILY_ROLLUP_TABLE
    WHERE event_date >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY event_date
    ORDER BY event_date
"""


class FakeJudge(DeepEvalBaseLLM):
    def __init__(self):
        super().__init__("fake-judge")

    def load_model(self):
        return None

    def generate(self, prompt, schema=None):
        raise AssertionError("unit tests must not call the judge")

    async def a_generate(self, prompt, schema=None):
        raise AssertionError("unit tests must not call the judge")

    def get_model_name(self):
        return "fake-judge"


def sql_case(sql=None, completion_time=1.0, **meta):
    return LLMTestCase(
        input=DAILY_REVENUE_PROMPT,
        actual_output=sql or NO_OUTPUT,
        expected_output="SELECT 1",
        completion_time=completion_time,
        metadata={"refused": sql is None, **meta},
    )
