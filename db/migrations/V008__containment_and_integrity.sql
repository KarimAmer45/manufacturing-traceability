-- First seven migrations preserve the supplied scaffold; improvements are append-only.
ALTER TABLE trace.load_step ADD CONSTRAINT station_interval_bounded
  CHECK (NOT lower_inf(in_station) AND lower_inc(in_station) AND NOT upper_inc(in_station));

-- Lock the tank row before maintaining its analysis timeline. Recompute against committed
-- rows under READ COMMITTED; concurrent conflicting writes still fail closed via exclusion.
CREATE OR REPLACE FUNCTION lab.set_analysis_validity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, lab AS $$
DECLARE next_sample timestamptz;
BEGIN
  PERFORM 1 FROM ref.tank WHERE tank_id = NEW.tank_id FOR UPDATE;
  IF EXISTS (SELECT 1 FROM lab.analysis WHERE tank_id = NEW.tank_id AND sampled_at = NEW.sampled_at) THEN
    NEW.valid_during := tstzrange(NEW.sampled_at, NULL, '[)');
    RETURN NEW;
  END IF;
  SELECT min(sampled_at) INTO next_sample FROM lab.analysis
    WHERE tank_id = NEW.tank_id AND sampled_at > NEW.sampled_at;
  NEW.valid_during := tstzrange(NEW.sampled_at, next_sample, '[)');
  UPDATE lab.analysis SET valid_during = tstzrange(lower(valid_during), NEW.sampled_at, '[)')
    WHERE tank_id = NEW.tank_id AND valid_during @> NEW.sampled_at;
  RETURN NEW;
END $$;

-- Timeline keys are immutable; corrections belong in analysis_result. Ingest/admin may
-- delete deliberately, but must repair validity periods afterwards (not a normal app flow).
CREATE FUNCTION lab.guard_analysis_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.tank_id, NEW.sampled_at) IS DISTINCT FROM (OLD.tank_id, OLD.sampled_at) THEN
    RAISE EXCEPTION 'Analysis tank and sampling time are immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER analysis_key_guard BEFORE UPDATE ON lab.analysis
  FOR EACH ROW EXECUTE FUNCTION lab.guard_analysis_key();

-- Count an affected lot once, even if it overlaps several bad analyses.
CREATE VIEW trace.v_containment_detail AS
WITH windows AS (
  SELECT tank_id, range_agg(valid_during) AS periods FROM lab.v_out_of_spec_period GROUP BY tank_id
), hits AS (
  SELECT DISTINCT f.lot_id FROM windows w
  CROSS JOIN LATERAL unnest(w.periods) p(win)
  CROSS JOIN LATERAL trace.forward_trace(w.tank_id, p.win) f
)
SELECT l.lot_id, l.lot_no, l.status, l.origin, l.qty_pcs AS lot_qty_pcs,
       c.code AS customer_code, d.delivery_no, d.shipped_at, dl.qty_pcs AS shipped_qty_pcs
FROM hits h JOIN trace.lot l USING (lot_id)
JOIN trace.customer_order o USING (order_id)
JOIN ref.customer c ON c.customer_id = o.customer_id
LEFT JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
LEFT JOIN trace.delivery d USING (delivery_id);

CREATE VIEW trace.v_containment_report AS
WITH lots AS (
  SELECT DISTINCT customer_code, lot_id, lot_no, status, origin, lot_qty_pcs
  FROM trace.v_containment_detail
), lot_counts AS (
  SELECT customer_code, count(*) AS affected_lots,
         count(*) FILTER (WHERE origin = 'receipt') AS receipt_lots,
         count(*) FILTER (WHERE origin <> 'receipt') AS descendant_lots,
         sum(lot_qty_pcs) FILTER (WHERE origin = 'receipt') AS affected_receipt_pieces,
         array_agg(lot_no ORDER BY lot_no) AS lot_numbers
  FROM lots GROUP BY customer_code
), shipments AS (
  SELECT customer_code, count(DISTINCT lot_id) AS shipped_lots,
         coalesce(sum(shipped_qty_pcs), 0) AS shipped_pieces,
         array_agg(DISTINCT delivery_no ORDER BY delivery_no) FILTER (WHERE delivery_no IS NOT NULL) AS delivery_notes
  FROM trace.v_containment_detail WHERE delivery_no IS NOT NULL GROUP BY customer_code
)
SELECT l.*, coalesce(s.shipped_lots, 0) AS shipped_lots,
       coalesce(s.shipped_pieces, 0) AS shipped_pieces,
       coalesce(s.delivery_notes, ARRAY[]::text[]) AS delivery_notes
FROM lot_counts l LEFT JOIN shipments s USING (customer_code);

CREATE VIEW bosch.v_part_path AS
SELECT p.part_id, p.response,
       string_agg(s.station_code, ' > ' ORDER BY s.t_first, s.t_last, s.station_code) AS station_path
FROM bosch.part p JOIN bosch.station_pass s USING (part_id)
GROUP BY p.part_id, p.response;

CREATE VIEW bosch.v_top_paths AS
SELECT station_path, count(*) AS parts, sum(response) AS failures,
       round(100.0 * avg(response), 4) AS failure_pct
FROM bosch.v_part_path GROUP BY station_path ORDER BY parts DESC, station_path LIMIT 10;

-- The tick conversion is a published hypothesis, not a recovered calendar timestamp.
CREATE VIEW bosch.v_daily_activity AS
SELECT floor(t_first / (16.75 / 7.0))::int AS relative_day,
       count(*) AS station_passes, count(DISTINCT part_id) AS parts
FROM bosch.station_pass GROUP BY 1;
