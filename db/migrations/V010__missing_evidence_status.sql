-- V010 missing bath evidence is reported, never silently dropped.
-- Before this migration a dwell without an analysis in force returned a NULL status, a missing
-- nickel result simply did not appear, and the last analysis of a tank stayed "in force" forever.
-- Every plating dwell now yields explicit evidence rows with one of these statuses:
--   OK / LOW / HIGH  result compared with the limit valid on the sampling day (see lab.v_result_status)
--   NO_LIMIT         a result exists, but no limit applies to it on the sampling day
--   MISSING          a parameter that has a limit for the process on the sampling day has no result
--   STALE            an OK result from an analysis older than the allowed age while the carrier was in the bath
--   NO_ANALYSIS      part of the dwell is not covered by any analysis of that tank

-- Allowed analysis age per process. Without a row the default of lab.max_analysis_age() applies:
-- 4 days. The simulated line samples once per working day at 06:00, so Friday's sample is in force
-- until Monday 06:00 (3 days); 4 days also covers a bank holiday Monday. Older evidence is STALE.
CREATE TABLE lab.analysis_max_age (
  process_code text     PRIMARY KEY REFERENCES ref.process,
  max_age      interval NOT NULL CHECK (max_age > interval '0'),
  reason       text     NOT NULL
);
CREATE TRIGGER analysis_max_age_audit AFTER INSERT OR UPDATE OR DELETE ON lab.analysis_max_age
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('process_code');

CREATE FUNCTION lab.max_analysis_age(p_process_code text) RETURNS interval
LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT max_age FROM lab.analysis_max_age WHERE process_code = p_process_code),
                  interval '4 days');
$$;

-- One row per plating dwell, analysis in force during it and parameter, plus one NO_ANALYSIS row
-- for each part of a dwell that no analysis covers. evidence_period is the part of the dwell the
-- row speaks for; analysis_age is measured at the end of that period.
CREATE VIEW trace.v_dwell_evidence AS
SELECT st.step_id, st.load_id, s.code AS station_code, t.tank_id, t.code AS tank_code, t.process_code,
       st.in_station, st.in_station * a.valid_during AS evidence_period,
       a.analysis_id, a.sampled_at,
       upper(st.in_station * a.valid_during) - a.sampled_at AS analysis_age,
       e.parameter_code, e.value,
       CASE
         WHEN e.status = 'OK'
          AND upper(st.in_station * a.valid_during) - a.sampled_at > lab.max_analysis_age(t.process_code)
           THEN 'STALE'
         ELSE coalesce(e.status, 'MISSING')   -- an analysis without any result is missing evidence too
       END AS status
  FROM trace.load_step st
  JOIN ref.station s  ON s.station_id = st.station_id
  JOIN ref.tank t     ON t.tank_id = s.tank_id AND t.process_code IS NOT NULL
  JOIN lab.analysis a ON a.tank_id = t.tank_id AND a.valid_during && st.in_station
  LEFT JOIN LATERAL (
    SELECT r.parameter_code, r.value, r.status
      FROM lab.v_result_status r
     WHERE r.analysis_id = a.analysis_id
    UNION ALL
    SELECT l.parameter_code, NULL::numeric, 'MISSING'
      FROM lab.parameter_limit l
     WHERE l.process_code = t.process_code
       AND l.valid_during @> (a.sampled_at AT TIME ZONE 'Europe/Berlin')::date
       AND NOT EXISTS (SELECT 1 FROM lab.analysis_result r
                        WHERE r.analysis_id = a.analysis_id AND r.parameter_code = l.parameter_code)
  ) e ON true
UNION ALL
SELECT st.step_id, st.load_id, s.code, t.tank_id, t.code, t.process_code,
       st.in_station, g.period,
       NULL::bigint, NULL::timestamptz, NULL::interval, NULL::text, NULL::numeric, 'NO_ANALYSIS'
  FROM trace.load_step st
  JOIN ref.station s ON s.station_id = st.station_id
  JOIN ref.tank t    ON t.tank_id = s.tank_id AND t.process_code IS NOT NULL
  CROSS JOIN LATERAL (
    SELECT tstzmultirange(st.in_station) - coalesce(range_agg(a.valid_during), '{}'::tstzmultirange) AS gaps
      FROM lab.analysis a
     WHERE a.tank_id = t.tank_id AND a.valid_during && st.in_station
  ) x
  CROSS JOIN LATERAL unnest(x.gaps) AS g(period);

-- Backward trace now reads the evidence view, so uncovered dwells and missing parameters appear as
-- rows with a status. The first eleven columns are unchanged; two columns are added at the end.
DROP FUNCTION trace.backward_trace(bigint);
CREATE FUNCTION trace.backward_trace(p_lot_id bigint)
RETURNS TABLE (lot_no text, relation text, load_id bigint, carrier_no text, station_code text,
               tank_code text, in_station tstzrange, analysis_id bigint, parameter_code text,
               value numeric, status text, evidence_period tstzrange, analysis_age interval)
LANGUAGE sql STABLE AS $$
  SELECT l.lot_no, f.relation, ld.load_id, ld.carrier_no, e.station_code, e.tank_code, e.in_station,
         e.analysis_id, e.parameter_code, e.value, e.status, e.evidence_period, e.analysis_age
    FROM trace.lot_family(p_lot_id) f
    JOIN trace.lot l              ON l.lot_id = f.lot_id
    JOIN trace.load_lot ll        ON ll.lot_id = l.lot_id
    JOIN trace.load ld            ON ld.load_id = ll.load_id
    -- OFFSET 0 keeps this a per-carrier lookup instead of evaluating the view for every dwell
    CROSS JOIN LATERAL (SELECT * FROM trace.v_dwell_evidence d WHERE d.load_id = ld.load_id OFFSET 0) e
   WHERE f.relation IN ('self','ancestor')
   ORDER BY lower(e.in_station), e.tank_code, lower(e.evidence_period), e.parameter_code;
$$;

-- Unknown exposure: lots (and their rework/split descendants) plated while the bath evidence was
-- absent, stale or incomplete. This is a separate review list; it does not change the containment
-- report, which only contains lots plated during a documented out-of-limit period.
CREATE VIEW trace.v_unknown_exposure AS
WITH gap_lots AS (
  SELECT ll.lot_id, array_agg(DISTINCT e.status) AS reasons
    FROM trace.v_dwell_evidence e
    JOIN trace.load_lot ll ON ll.load_id = e.load_id
   WHERE e.status IN ('NO_ANALYSIS', 'STALE', 'MISSING')
   GROUP BY ll.lot_id
), fam AS (
  SELECT f.lot_id, f.relation, r.reason
    FROM gap_lots g
    CROSS JOIN LATERAL trace.lot_family(g.lot_id) f
    CROSS JOIN LATERAL unnest(g.reasons) AS r(reason)
   WHERE f.relation IN ('self', 'descendant')
), lots AS (
  SELECT lot_id,
         CASE WHEN bool_or(relation = 'self') THEN 'direct' ELSE 'descendant' END AS trace_relation,
         array_agg(DISTINCT reason ORDER BY reason) AS reasons
    FROM fam GROUP BY lot_id
)
SELECT l.lot_id, l.lot_no, l.status AS lot_status, l.origin, x.trace_relation, x.reasons,
       coalesce(rc.code, oc.code) AS customer_code, oc.code AS order_customer_code,
       d.delivery_no, d.shipped_at, dl.qty_pcs AS shipped_qty_pcs, l.qty_pcs AS lot_qty_pcs
  FROM lots x
  JOIN trace.lot l               ON l.lot_id = x.lot_id
  JOIN trace.customer_order o    ON o.order_id = l.order_id
  JOIN ref.customer oc           ON oc.customer_id = o.customer_id
  LEFT JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
  LEFT JOIN trace.delivery d      ON d.delivery_id = dl.delivery_id
  LEFT JOIN ref.customer rc       ON rc.customer_id = d.customer_id;
