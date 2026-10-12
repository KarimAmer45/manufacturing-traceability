-- Quality's contact list. Shipments are listed under the customer on the delivery note; affected
-- pieces count each genealogy once, so rework children are not double-counted.
SELECT * FROM trace.v_containment_report ORDER BY customer_code;
-- Unknown exposure: lots plated while bath evidence was absent, stale or incomplete. A separate
-- review list, not part of the containment figures.
SELECT * FROM trace.v_unknown_exposure ORDER BY customer_code, lot_no;
-- Most frequent observed paths; timestamp ties are ordered deterministically, not resolved physically.
SELECT * FROM bosch.v_top_paths;
-- Relative days only: no calendar origin is available for anonymised Bosch ticks.
SELECT * FROM bosch.v_daily_activity ORDER BY relative_day;
-- Coverage gaps must remain visible; absence of an analysis is not evidence of a good bath.
SELECT l.lot_no, st.step_id, st.in_station
FROM trace.load_step st JOIN ref.station s USING (station_id)
JOIN trace.load_lot ll USING (load_id) JOIN trace.lot l USING (lot_id)
WHERE NOT EXISTS (SELECT 1 FROM lab.analysis a
  WHERE a.tank_id = s.tank_id AND a.valid_during && st.in_station);
