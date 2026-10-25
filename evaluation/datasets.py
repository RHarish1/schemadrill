"""Deterministic local benchmark datasets and their ground-truth questions."""

# SQL fixtures and compact question definitions intentionally preserve readable SQL lines.
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from psycopg import Connection

Comparison = Literal["unordered", "ordered"]


@dataclass(frozen=True)
class BenchmarkQuestion:
    question_id: str
    db: str
    question: str
    gold_sql: str
    difficulty: Literal["easy", "medium", "hard"]
    comparison: Comparison
    explanation: str


@dataclass(frozen=True)
class BenchmarkDataset:
    dataset_id: str
    version: str
    dialect: str
    schema_sql: str
    _questions: tuple[BenchmarkQuestion, ...]

    def initialize(self, connection: Connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(self.schema_sql)
        connection.commit()

    def seed(self, connection: Connection, seed: int = 0) -> None:
        del seed
        self.initialize(connection)

    def reset(self, connection: Connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {self.dataset_id} CASCADE")
        connection.commit()

    def questions(self) -> tuple[BenchmarkQuestion, ...]:
        return self._questions


def _question(
    dataset: str,
    number: int,
    text: str,
    sql: str,
    difficulty: Literal["easy", "medium", "hard"],
    explanation: str,
) -> BenchmarkQuestion:
    return BenchmarkQuestion(
        question_id=f"{dataset}-{number:03d}",
        db=dataset,
        question=text,
        gold_sql=sql,
        difficulty=difficulty,
        comparison="ordered" if "ORDER BY" in sql.upper() else "unordered",
        explanation=explanation,
    )


ECOMMERCE_SQL = """
CREATE SCHEMA IF NOT EXISTS ecommerce;
CREATE TABLE IF NOT EXISTS ecommerce.customers (
  customer_id integer PRIMARY KEY, name text NOT NULL, city text, created_at date NOT NULL
);
CREATE TABLE IF NOT EXISTS ecommerce.products (
  product_id integer PRIMARY KEY, name text NOT NULL, category text NOT NULL,
  price numeric(10,2) NOT NULL CHECK (price >= 0)
);
CREATE TABLE IF NOT EXISTS ecommerce.orders (
  order_id integer PRIMARY KEY, customer_id integer NOT NULL REFERENCES ecommerce.customers,
  order_date date NOT NULL, status text NOT NULL
);
CREATE TABLE IF NOT EXISTS ecommerce.order_items (
  order_id integer REFERENCES ecommerce.orders, product_id integer REFERENCES ecommerce.products,
  quantity integer NOT NULL CHECK (quantity > 0), unit_price numeric(10,2) NOT NULL,
  PRIMARY KEY (order_id, product_id)
);
CREATE TABLE IF NOT EXISTS ecommerce.payments (
  payment_id integer PRIMARY KEY, order_id integer REFERENCES ecommerce.orders,
  amount numeric(10,2) NOT NULL, payment_date date, status text NOT NULL
);
CREATE TABLE IF NOT EXISTS ecommerce.shipments (
  shipment_id integer PRIMARY KEY, order_id integer REFERENCES ecommerce.orders,
  shipped_at date, delivered_at date, status text NOT NULL
);
INSERT INTO ecommerce.customers VALUES
 (1,'Ada','London','2024-01-01'),(2,'Grace','New York','2024-01-02'),
 (3,'Linus',NULL,'2024-02-01'),(4,'Margaret','London','2024-03-01')
 ON CONFLICT DO NOTHING;
INSERT INTO ecommerce.products VALUES
 (1,'Keyboard','hardware',80),(2,'Mouse','hardware',25),(3,'Notebook','stationery',8),
 (4,'Keyboard','accessories',60),(5,'Monitor','hardware',220)
 ON CONFLICT DO NOTHING;
INSERT INTO ecommerce.orders VALUES
 (101,1,'2024-04-01','completed'),(102,1,'2024-04-03','completed'),
 (103,2,'2024-04-05','cancelled'),(104,3,'2024-05-01','completed')
 ON CONFLICT DO NOTHING;
INSERT INTO ecommerce.order_items VALUES
 (101,1,1,80),(101,2,2,25),(102,3,5,8),(103,5,1,220),(104,4,1,60)
 ON CONFLICT DO NOTHING;
INSERT INTO ecommerce.payments VALUES
 (1,101,130,'2024-04-01','successful'),(2,102,20,'2024-04-03','successful'),
 (3,104,30,'2024-05-01','partial')
 ON CONFLICT DO NOTHING;
INSERT INTO ecommerce.shipments VALUES
 (1,101,'2024-04-02','2024-04-04','delivered'),(2,102,NULL,NULL,'pending'),
 (3,103,NULL,NULL,'cancelled')
 ON CONFLICT DO NOTHING;
"""

ORGANIZATION_SQL = """
CREATE SCHEMA IF NOT EXISTS organization;
CREATE TABLE IF NOT EXISTS organization.departments (
 department_id integer PRIMARY KEY, name text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS organization.employees (
 employee_id integer PRIMARY KEY, name text NOT NULL,
 manager_id integer REFERENCES organization.employees,
 department_id integer REFERENCES organization.departments,
 salary numeric(12,2), hired_at date NOT NULL
);
CREATE TABLE IF NOT EXISTS organization.projects (
 project_id integer PRIMARY KEY, name text NOT NULL,
 department_id integer REFERENCES organization.departments, budget numeric(14,2)
);
CREATE TABLE IF NOT EXISTS organization.assignments (
 employee_id integer REFERENCES organization.employees,
 project_id integer REFERENCES organization.projects,
 assigned_at date NOT NULL, hours_per_week numeric(5,2) NOT NULL,
 PRIMARY KEY (employee_id, project_id)
);
INSERT INTO organization.departments VALUES (1,'Engineering'),(2,'Research'),(3,'Operations')
 ON CONFLICT DO NOTHING;
INSERT INTO organization.employees VALUES
 (1,'Mina',NULL,1,150000,'2020-01-01'),(2,'Noah',1,1,100000,'2021-01-01'),
 (3,'Priya',1,1,110000,'2022-01-01'),(4,'Omar',NULL,2,90000,'2020-06-01'),
 (5,'Ruth',4,2,NULL,'2023-01-01'),(6,'Sven',NULL,3,70000,'2024-01-01')
 ON CONFLICT DO NOTHING;
INSERT INTO organization.projects VALUES
 (10,'Compiler',1,500000),(11,'ML Lab',2,300000),(12,'Unstaffed',3,100000)
 ON CONFLICT DO NOTHING;
INSERT INTO organization.assignments VALUES
 (1,10,'2024-01-01',10),(2,10,'2024-01-02',30),(3,11,'2024-01-03',20),(4,11,'2024-01-04',10)
 ON CONFLICT DO NOTHING;
"""

COMPLEX_SQL = """
CREATE SCHEMA IF NOT EXISTS complex_synthetic;
CREATE TABLE IF NOT EXISTS complex_synthetic.regions (
 region_id integer PRIMARY KEY, name text NOT NULL
);
CREATE TABLE IF NOT EXISTS complex_synthetic.accounts (
 tenant_id integer, account_id integer, name text NOT NULL, region_id integer REFERENCES complex_synthetic.regions,
 PRIMARY KEY (tenant_id, account_id)
);
CREATE TABLE IF NOT EXISTS complex_synthetic.users (
 tenant_id integer, user_id integer, name text NOT NULL, PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS complex_synthetic.memberships (
 tenant_id integer, account_id integer, user_id integer, role text NOT NULL,
 PRIMARY KEY (tenant_id, account_id, user_id),
 FOREIGN KEY (tenant_id, account_id) REFERENCES complex_synthetic.accounts,
 FOREIGN KEY (tenant_id, user_id) REFERENCES complex_synthetic.users
);
CREATE TABLE IF NOT EXISTS complex_synthetic.projects (
 project_id integer PRIMARY KEY, tenant_id integer, account_id integer, name text NOT NULL,
 FOREIGN KEY (tenant_id, account_id) REFERENCES complex_synthetic.accounts
);
CREATE TABLE IF NOT EXISTS complex_synthetic.events (
 event_id integer PRIMARY KEY, tenant_id integer, user_id integer,
 project_id integer REFERENCES complex_synthetic.projects, actor_name text, event_type text NOT NULL,
 occurred_at timestamp NOT NULL,
 FOREIGN KEY (tenant_id, user_id) REFERENCES complex_synthetic.users
);
INSERT INTO complex_synthetic.regions VALUES (1,'East'),(2,'West') ON CONFLICT DO NOTHING;
INSERT INTO complex_synthetic.accounts VALUES
 (1,10,'Acme',1),(1,20,'Beta',2),(2,10,'Acme Other',1) ON CONFLICT DO NOTHING;
INSERT INTO complex_synthetic.users VALUES
 (1,100,'Alice'),(1,200,'Bob'),(2,100,'Carol') ON CONFLICT DO NOTHING;
INSERT INTO complex_synthetic.memberships VALUES
 (1,10,100,'owner'),(1,20,200,'member'),(2,10,100,'owner') ON CONFLICT DO NOTHING;
INSERT INTO complex_synthetic.projects VALUES
 (1000,1,10,'Alpha'),(1001,1,20,'Beta Project'),(1002,2,10,'Other Alpha')
 ON CONFLICT DO NOTHING;
INSERT INTO complex_synthetic.events VALUES
 (1,1,100,1000,'Alice','login','2024-01-01'),
 (2,1,200,1001,'Bob','deploy','2024-01-02'),
 (3,2,100,1002,'Carol','login','2024-01-03'),
 (4,1,100,NULL,'Alice','logout','2024-01-04')
 ON CONFLICT DO NOTHING;
"""


DATASETS = {
    "chinook": BenchmarkDataset(
        "chinook",
        "1",
        "postgresql",
        "",
        tuple(),
    ),
    "ecommerce": BenchmarkDataset(
        "ecommerce",
        "1",
        "postgresql",
        ECOMMERCE_SQL,
        (
            _question(
                "ecommerce",
                1,
                "List customers with no orders.",
                "SELECT c.name FROM ecommerce.customers c LEFT JOIN ecommerce.orders o ON o.customer_id=c.customer_id WHERE o.order_id IS NULL ORDER BY c.customer_id;",
                "easy",
                "Tests LEFT JOIN NULL filtering.",
            ),
            _question(
                "ecommerce",
                2,
                "List products that were never ordered.",
                "SELECT p.name FROM ecommerce.products p LEFT JOIN ecommerce.order_items i ON i.product_id=p.product_id WHERE i.product_id IS NULL ORDER BY p.product_id;",
                "easy",
                "Tests anti-join semantics.",
            ),
            _question(
                "ecommerce",
                3,
                "Show completed order totals.",
                "SELECT o.order_id, SUM(i.quantity*i.unit_price) AS total FROM ecommerce.orders o JOIN ecommerce.order_items i ON i.order_id=o.order_id WHERE o.status='completed' GROUP BY o.order_id ORDER BY o.order_id;",
                "medium",
                "Tests filtered aggregation.",
            ),
            _question(
                "ecommerce",
                4,
                "Which categories have an average price above 50?",
                "SELECT category FROM ecommerce.products GROUP BY category HAVING AVG(price)>50 ORDER BY category;",
                "medium",
                "Tests HAVING.",
            ),
            _question(
                "ecommerce",
                5,
                "Show orders whose successful payment differs from item total.",
                "SELECT o.order_id FROM ecommerce.orders o JOIN ecommerce.order_items i ON i.order_id=o.order_id LEFT JOIN ecommerce.payments p ON p.order_id=o.order_id AND p.status='successful' GROUP BY o.order_id HAVING SUM(i.quantity*i.unit_price) <> COALESCE(SUM(p.amount),0) ORDER BY o.order_id;",
                "hard",
                "Tests payment/order-total distinction.",
            ),
            _question(
                "ecommerce",
                6,
                "Show unshipped orders in April 2024.",
                "SELECT order_id FROM ecommerce.orders WHERE order_date BETWEEN '2024-04-01' AND '2024-04-30' AND order_id IN (SELECT order_id FROM ecommerce.shipments WHERE shipped_at IS NULL) ORDER BY order_id;",
                "hard",
                "Tests dates and NULL shipment state.",
            ),
        ),
    ),
    "organization": BenchmarkDataset(
        "organization",
        "1",
        "postgresql",
        ORGANIZATION_SQL,
        (
            _question(
                "organization",
                1,
                "List employees without managers.",
                "SELECT name FROM organization.employees WHERE manager_id IS NULL ORDER BY employee_id;",
                "easy",
                "Tests nullable self-reference.",
            ),
            _question(
                "organization",
                2,
                "List projects with no assignments.",
                "SELECT p.name FROM organization.projects p LEFT JOIN organization.assignments a ON a.project_id=p.project_id WHERE a.project_id IS NULL ORDER BY p.project_id;",
                "easy",
                "Tests empty many-to-many side.",
            ),
            _question(
                "organization",
                3,
                "Count employees in each department.",
                "SELECT d.name, COUNT(e.employee_id) FROM organization.departments d LEFT JOIN organization.employees e ON e.department_id=d.department_id GROUP BY d.department_id,d.name ORDER BY d.department_id;",
                "medium",
                "Tests department aggregation.",
            ),
            _question(
                "organization",
                4,
                "List employees earning above their department average.",
                "SELECT e.name FROM organization.employees e WHERE e.salary > (SELECT AVG(e2.salary) FROM organization.employees e2 WHERE e2.department_id=e.department_id) ORDER BY e.employee_id;",
                "hard",
                "Tests correlated subquery and NULL salary.",
            ),
            _question(
                "organization",
                5,
                "List managers and their direct report counts.",
                "SELECT m.name, COUNT(e.employee_id) FROM organization.employees m JOIN organization.employees e ON e.manager_id=m.employee_id GROUP BY m.employee_id,m.name ORDER BY m.employee_id;",
                "medium",
                "Tests self-join.",
            ),
            _question(
                "organization",
                6,
                "List employees assigned outside their department.",
                "SELECT e.name FROM organization.employees e JOIN organization.assignments a ON a.employee_id=e.employee_id JOIN organization.projects p ON p.project_id=a.project_id WHERE p.department_id<>e.department_id ORDER BY e.employee_id;",
                "hard",
                "Tests many-to-many relationship selection.",
            ),
        ),
    ),
    "complex_synthetic": BenchmarkDataset(
        "complex_synthetic",
        "1",
        "postgresql",
        COMPLEX_SQL,
        (
            _question(
                "complex_synthetic",
                1,
                "List users in tenant 1 account 10.",
                "SELECT u.name FROM complex_synthetic.users u JOIN complex_synthetic.memberships m ON m.tenant_id=u.tenant_id AND m.user_id=u.user_id WHERE m.tenant_id=1 AND m.account_id=10 ORDER BY u.user_id;",
                "easy",
                "Requires tenant-preserving composite join.",
            ),
            _question(
                "complex_synthetic",
                2,
                "List projects in the East region.",
                "SELECT p.name FROM complex_synthetic.projects p JOIN complex_synthetic.accounts a ON a.tenant_id=p.tenant_id AND a.account_id=p.account_id JOIN complex_synthetic.regions r ON r.region_id=a.region_id WHERE r.name='East' ORDER BY p.project_id;",
                "medium",
                "Tests multi-hop composite relationship.",
            ),
            _question(
                "complex_synthetic",
                3,
                "List direct login event users.",
                "SELECT u.name FROM complex_synthetic.events e JOIN complex_synthetic.users u ON u.tenant_id=e.tenant_id AND u.user_id=e.user_id WHERE e.event_type='login' ORDER BY e.event_id;",
                "medium",
                "Uses direct event attribution, not actor_name.",
            ),
            _question(
                "complex_synthetic",
                4,
                "Count members per tenant- scoped account.",
                "SELECT m.tenant_id,m.account_id,COUNT(*) FROM complex_synthetic.memberships m GROUP BY m.tenant_id,m.account_id ORDER BY m.tenant_id,m.account_id;",
                "hard",
                "Tests composite identity and grouping.",
            ),
            _question(
                "complex_synthetic",
                5,
                "List events without a project.",
                "SELECT event_id FROM complex_synthetic.events WHERE project_id IS NULL ORDER BY event_id;",
                "easy",
                "Tests nullable multi-hop relationship.",
            ),
            _question(
                "complex_synthetic",
                6,
                "List account names for tenant 1 events through projects.",
                "SELECT DISTINCT a.name FROM complex_synthetic.events e JOIN complex_synthetic.projects p ON p.project_id=e.project_id AND p.tenant_id=e.tenant_id JOIN complex_synthetic.accounts a ON a.tenant_id=p.tenant_id AND a.account_id=p.account_id WHERE e.tenant_id=1 ORDER BY a.name;",
                "hard",
                "Rejects cross-tenant account_id-only joins.",
            ),
        ),
    ),
}


def get_dataset(dataset_id: str) -> BenchmarkDataset:
    try:
        return DATASETS[dataset_id]
    except KeyError as error:
        choices = ", ".join(sorted(DATASETS))
        raise ValueError(
            f"Unknown benchmark dataset {dataset_id!r}; choose from {choices}"
        ) from error


def dataset_questions(dataset_id: str) -> list[dict[str, object]]:
    dataset = get_dataset(dataset_id)
    if dataset_id == "chinook":
        import json
        from pathlib import Path

        return json.loads(
            (Path(__file__).parent / "chinook" / "cases.json").read_text(encoding="utf-8")
        )
    return [question.__dict__.copy() for question in dataset.questions()]
