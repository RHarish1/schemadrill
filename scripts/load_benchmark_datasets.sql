-- SchemaDrill synthetic benchmark fixtures.
-- Additive/idempotent; intended for a dedicated benchmark database or approved branch.
-- Generated from evaluation.datasets; do not run against an unintended database.


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

GRANT USAGE ON SCHEMA ecommerce, organization, complex_synthetic TO schemadrill_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA ecommerce, organization, complex_synthetic TO schemadrill_readonly;
