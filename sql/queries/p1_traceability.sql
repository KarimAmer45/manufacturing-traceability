-- Project 1 showcase queries. Each one answers a question a quality engineer actually asks.

-- Q1. When did the ZnNi bath run outside its operating window?
SELECT tank_code, analysis_id, valid_during, findings
  FROM lab.v_out_of_spec_period
 ORDER BY valid_during;

-- Q2. Complaint received: what was the bath state while this lot was plated? (backward trace)
--     Everything that is not plainly OK: LOW/HIGH results, NO_LIMIT (result without a limit, e.g. COD),
--     MISSING (limited parameter without a result), STALE (analysis older than lab.max_analysis_age)
--     and NO_ANALYSIS (part of the dwell with no analysis in force). Results are compared with the
--     limit version valid on the sampling day.
SELECT lot_no, relation, load_id, station_code, lower(in_station) AS plated_from,
       analysis_id, parameter_code, value, status, analysis_age
  FROM trace.backward_trace((SELECT lot_id FROM trace.complaint ORDER BY complaint_id LIMIT 1))
 WHERE status <> 'OK';

-- Q3. Containment: everything plated during any out of spec period, and where it went (forward trace).
--     range_agg merges touching periods first, so back to back bad analyses become one window.
WITH w AS (
  SELECT tank_id, range_agg(valid_during) AS periods
    FROM lab.v_out_of_spec_period
   GROUP BY tank_id
)
--     customer_code is the recipient of the delivery note (the ordering customer if not shipped).
SELECT f.customer_code, f.order_customer_code, f.delivery_no, f.shipped_at, f.lot_no, f.relation,
       f.lot_status, f.shipped_qty_pcs, f.lot_qty_pcs
  FROM w
 CROSS JOIN LATERAL unnest(w.periods) AS p(win)
 CROSS JOIN LATERAL trace.forward_trace(w.tank_id, p.win) f
 ORDER BY f.customer_code, f.shipped_at NULLS LAST;

-- Q4. Rework genealogy of a lot (recursive CTE with cycle protection inside lot_family)
SELECT * FROM trace.lot_family((SELECT lot_id FROM trace.lot WHERE origin = 'rework' ORDER BY lot_id LIMIT 1));

-- Q5. The physics is enforced: try to put a second barrel into an occupied station. This must fail
--     with "conflicting key value violates exclusion constraint station_one_carrier".
-- INSERT INTO trace.load_step (load_id, station_id, in_station)
-- SELECT load_id + 1, station_id, in_station FROM trace.load_step ORDER BY step_id LIMIT 1;

-- Q6. Which measurement values were changed after the fact, under which database login? (audit trail)
--     login_user is the PostgreSQL login that ran the statement; changed_by is the name the
--     application set in app.user, a label the client chooses.
SELECT changed_at, login_user, changed_by, row_pk ->> 'measurement_id' AS measurement_id,
       old_row ->> 'value' AS old_value, new_row ->> 'value' AS new_value
  FROM audit.change_log
 WHERE table_name = 'trace.measurement' AND operation = 'UPDATE'
 ORDER BY changed_at DESC;

-- Q7. First pass yield per coating spec and month
SELECT spec_code, date_trunc('month', received_at)::date AS month,
       count(*) FILTER (WHERE status IN ('shipped','released')) AS passed,
       count(*) AS lots,
       round(100.0 * count(*) FILTER (WHERE status IN ('shipped','released')) / count(*), 1) AS fpy_pct
  FROM trace.lot
 WHERE origin = 'receipt'
 GROUP BY 1, 2
 ORDER BY 1, 2;

-- Q8. Real data at scale (Bosch): failure rate per station, stations with enough traffic only
SELECT sp.station_code, count(*) AS parts, sum(p.response) AS failed,
       round(100.0 * avg(p.response), 2) AS fail_pct
  FROM bosch.station_pass sp
  JOIN bosch.part p USING (part_id)
 GROUP BY sp.station_code
HAVING count(*) > 10000
 ORDER BY fail_pct DESC, sp.station_code;

-- Q9. Bosch "forward trace": parts that passed the same station within 0.05 ticks of a failed part
--     (first 100 pairs in a fixed order, so repeated runs return and time the same rows)
SELECT f.part_id AS failed_part, n.part_id AS neighbour, n.station_code, n.t_first
  FROM bosch.station_pass f
  JOIN bosch.part fp ON fp.part_id = f.part_id AND fp.response = 1
  JOIN bosch.station_pass n
    ON n.station_code = f.station_code
   AND n.part_id <> f.part_id
   AND n.t_first BETWEEN f.t_first - 0.05 AND f.t_first + 0.05
 WHERE f.station_code = 'L3_S32'
 ORDER BY f.part_id, n.part_id
 LIMIT 100;
