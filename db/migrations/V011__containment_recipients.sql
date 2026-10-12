-- V011 containment names the customer who received the parts and counts pieces by trace relation.
-- * Shipped rows are attributed to trace.delivery.customer_id (the recipient on the delivery note);
--   the ordering customer stays available as order_customer_code.
-- * A lot is 'direct' if it was itself plated during a bad period, 'descendant' if it is only
--   affected through a parent. Pieces are counted once per genealogy: a lot's own quantity counts
--   unless its parent is already affected (then they are the same pieces at another stage).
-- * Shipped quantity and lot quantity are separate columns.

DROP VIEW trace.v_containment_report;
DROP VIEW trace.v_containment_detail;
DROP FUNCTION trace.forward_trace(int, tstzrange);

-- customer_code is the delivery recipient for shipped rows and the ordering customer otherwise.
CREATE FUNCTION trace.forward_trace(p_tank_id int, p_window tstzrange)
RETURNS TABLE (lot_id bigint, lot_no text, relation text, lot_status text, customer_code text,
               order_customer_code text, delivery_no text, shipped_at timestamptz,
               shipped_qty_pcs int, lot_qty_pcs int)
LANGUAGE sql STABLE AS $$
  WITH hit_lots AS (
    SELECT DISTINCT ll.lot_id
      FROM trace.load_step st
      JOIN ref.station s     ON s.station_id = st.station_id
      JOIN trace.load_lot ll ON ll.load_id = st.load_id
     WHERE s.tank_id = p_tank_id
       AND st.in_station && p_window
  ),
  affected AS (
    SELECT f.lot_id, f.relation
      FROM hit_lots h
      CROSS JOIN LATERAL trace.lot_family(h.lot_id) f
     WHERE f.relation IN ('self','descendant')
  )
  SELECT DISTINCT ON (a.lot_id, d.delivery_id)
         a.lot_id, l.lot_no, a.relation, l.status, coalesce(rc.code, oc.code), oc.code,
         d.delivery_no, d.shipped_at, dl.qty_pcs, l.qty_pcs
    FROM affected a
    JOIN trace.lot l                ON l.lot_id = a.lot_id
    JOIN trace.customer_order o     ON o.order_id = l.order_id
    JOIN ref.customer oc            ON oc.customer_id = o.customer_id
    LEFT JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
    LEFT JOIN trace.delivery d      ON d.delivery_id = dl.delivery_id
    LEFT JOIN ref.customer rc       ON rc.customer_id = d.customer_id
   ORDER BY a.lot_id, d.delivery_id, (a.relation = 'self') DESC;
$$;

-- Count an affected lot once, even if it overlaps several bad analyses.
CREATE VIEW trace.v_containment_detail AS
WITH windows AS (
  SELECT tank_id, range_agg(valid_during) AS periods FROM lab.v_out_of_spec_period GROUP BY tank_id
), hits AS (
  SELECT f.lot_id, CASE WHEN bool_or(f.relation = 'self') THEN 'direct' ELSE 'descendant' END AS trace_relation
    FROM windows w
   CROSS JOIN LATERAL unnest(w.periods) p(win)
   CROSS JOIN LATERAL trace.forward_trace(w.tank_id, p.win) f
   GROUP BY f.lot_id
)
SELECT l.lot_id, l.lot_no, l.status, l.origin, h.trace_relation, l.parent_lot_id,
       l.qty_pcs AS lot_qty_pcs,
       coalesce(rc.code, oc.code) AS customer_code, oc.code AS order_customer_code,
       d.delivery_no, d.shipped_at, dl.qty_pcs AS shipped_qty_pcs
  FROM hits h
  JOIN trace.lot l                ON l.lot_id = h.lot_id
  JOIN trace.customer_order o     ON o.order_id = l.order_id
  JOIN ref.customer oc            ON oc.customer_id = o.customer_id
  LEFT JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
  LEFT JOIN trace.delivery d      ON d.delivery_id = dl.delivery_id
  LEFT JOIN ref.customer rc       ON rc.customer_id = d.customer_id;

-- Quality's contact list. Affected lots are counted under the customer who ordered them,
-- shipments under the customer who received the delivery note.
CREATE VIEW trace.v_containment_report AS
WITH detail AS MATERIALIZED (
  SELECT * FROM trace.v_containment_detail
), lots AS (
  SELECT DISTINCT lot_id, lot_no, parent_lot_id, order_customer_code, trace_relation, lot_qty_pcs
    FROM detail
), lot_counts AS (
  SELECT order_customer_code AS customer_code, count(*) AS affected_lots,
         count(*) FILTER (WHERE trace_relation = 'direct') AS direct_lots,
         count(*) FILTER (WHERE trace_relation = 'descendant') AS descendant_lots,
         coalesce(sum(lot_qty_pcs) FILTER (
           WHERE parent_lot_id IS NULL OR parent_lot_id NOT IN (SELECT lot_id FROM lots)), 0) AS affected_pieces,
         array_agg(lot_no ORDER BY lot_no) AS lot_numbers
    FROM lots GROUP BY order_customer_code
), shipments AS (
  SELECT customer_code, count(DISTINCT lot_id) AS shipped_lots,
         sum(shipped_qty_pcs) AS shipped_pieces,
         array_agg(DISTINCT delivery_no ORDER BY delivery_no) AS delivery_notes
    FROM detail WHERE delivery_no IS NOT NULL GROUP BY customer_code
)
SELECT customer_code,
       coalesce(l.affected_lots, 0) AS affected_lots,
       coalesce(l.direct_lots, 0) AS direct_lots,
       coalesce(l.descendant_lots, 0) AS descendant_lots,
       coalesce(l.affected_pieces, 0) AS affected_pieces,
       coalesce(l.lot_numbers, ARRAY[]::text[]) AS lot_numbers,
       coalesce(s.shipped_lots, 0) AS shipped_lots,
       coalesce(s.shipped_pieces, 0) AS shipped_pieces,
       coalesce(s.delivery_notes, ARRAY[]::text[]) AS delivery_notes
  FROM lot_counts l FULL JOIN shipments s USING (customer_code);

-- A delivery note goes to the customer who ordered the lot. Checked whenever one of the four
-- linking columns changes. (If drop shipments to a third party were ever needed, this rule would be
-- relaxed; the containment views above already report the recipient.)
CREATE FUNCTION trace.check_delivery_customer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE bad record;
BEGIN
  IF TG_TABLE_NAME = 'delivery_lot' THEN
    SELECT d.delivery_no, l.lot_no INTO bad
      FROM trace.delivery d, trace.lot l JOIN trace.customer_order o ON o.order_id = l.order_id
     WHERE d.delivery_id = NEW.delivery_id AND l.lot_id = NEW.lot_id AND d.customer_id <> o.customer_id;
  ELSIF TG_TABLE_NAME = 'delivery' THEN
    SELECT d.delivery_no, l.lot_no INTO bad
      FROM trace.delivery d JOIN trace.delivery_lot dl ON dl.delivery_id = d.delivery_id
      JOIN trace.lot l ON l.lot_id = dl.lot_id JOIN trace.customer_order o ON o.order_id = l.order_id
     WHERE d.delivery_id = NEW.delivery_id AND d.customer_id <> o.customer_id LIMIT 1;
  ELSIF TG_TABLE_NAME = 'lot' THEN
    SELECT d.delivery_no, l.lot_no INTO bad
      FROM trace.delivery_lot dl JOIN trace.delivery d ON d.delivery_id = dl.delivery_id
      JOIN trace.lot l ON l.lot_id = dl.lot_id JOIN trace.customer_order o ON o.order_id = l.order_id
     WHERE dl.lot_id = NEW.lot_id AND d.customer_id <> o.customer_id LIMIT 1;
  ELSE  -- customer_order
    SELECT d.delivery_no, l.lot_no INTO bad
      FROM trace.lot l JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
      JOIN trace.delivery d ON d.delivery_id = dl.delivery_id
     WHERE l.order_id = NEW.order_id AND d.customer_id <> NEW.customer_id LIMIT 1;
  END IF;
  IF FOUND THEN
    RAISE EXCEPTION 'Delivery % for lot % is addressed to a different customer than the order', bad.delivery_no, bad.lot_no
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER delivery_lot_customer AFTER INSERT OR UPDATE ON trace.delivery_lot
  DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION trace.check_delivery_customer();
CREATE CONSTRAINT TRIGGER delivery_customer AFTER UPDATE OF customer_id ON trace.delivery
  DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION trace.check_delivery_customer();
CREATE CONSTRAINT TRIGGER lot_order_delivery_customer AFTER UPDATE OF order_id ON trace.lot
  DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION trace.check_delivery_customer();
CREATE CONSTRAINT TRIGGER order_delivery_customer AFTER UPDATE OF customer_id ON trace.customer_order
  DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION trace.check_delivery_customer();
