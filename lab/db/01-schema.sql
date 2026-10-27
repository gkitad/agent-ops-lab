CREATE TABLE customers (
  id serial PRIMARY KEY,
  name text NOT NULL,
  company text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO customers (name, company) VALUES
  ('Dana Whitfield', 'Lakeside Logistics'),
  ('James Okafor', 'Okafor Freight'),
  ('Priya Raman', 'Raman Cold Storage'),
  ('Luis Ortega', 'Ortega Distribution');
