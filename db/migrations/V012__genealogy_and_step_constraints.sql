-- V012 cheap physical-consistency rules that the exclusion constraints did not cover.
-- * A child lot (rework or split) keeps its parent's part and customer, its children together never
--   hold more pieces than the parent, and the genealogy has no cycles. Checked at commit
--   (DEFERRABLE INITIALLY DEFERRED), so a transaction may re-parent several lots in any order.
-- * A carrier's dwell in a station lies within the carrier run's start and finish.
-- Still not enforced (documented in docs/architecture.md): quantity conservation across carrier
-- allocations and deliveries, part mixing within a carrier, and reuse of a physical carrier number
-- in overlapping runs.

CREATE FUNCTION trace.check_lot_genealogy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  cur    record;
  parent record;
BEGIN
  -- Deferred events carry the row as it was when queued; check the row as it is now.
  SELECT l.lot_id, l.lot_no, l.part_id, l.qty_pcs, l.parent_lot_id, o.customer_id INTO cur
    FROM trace.lot l JOIN trace.customer_order o ON o.order_id = l.order_id
   WHERE l.lot_id = NEW.lot_id;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  IF cur.parent_lot_id IS NOT NULL THEN
    IF EXISTS (
      WITH RECURSIVE up (lot_id) AS (
        SELECT cur.parent_lot_id
        UNION
        SELECT l.parent_lot_id FROM trace.lot l JOIN up ON l.lot_id = up.lot_id WHERE l.parent_lot_id IS NOT NULL
      ) SELECT 1 FROM up WHERE up.lot_id = cur.lot_id) THEN
      RAISE EXCEPTION 'Lot % would become its own ancestor', cur.lot_no
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT l.lot_no, l.part_id, l.qty_pcs, o.customer_id INTO parent
      FROM trace.lot l JOIN trace.customer_order o ON o.order_id = l.order_id
     WHERE l.lot_id = cur.parent_lot_id;
    IF parent.part_id <> cur.part_id OR parent.customer_id <> cur.customer_id THEN
      RAISE EXCEPTION 'Lot % must keep the part and customer of its parent lot %', cur.lot_no, parent.lot_no
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (SELECT sum(qty_pcs) FROM trace.lot WHERE parent_lot_id = cur.parent_lot_id) > parent.qty_pcs THEN
      RAISE EXCEPTION 'Child lots of % hold more pieces than the parent lot (% pcs)', parent.lot_no, parent.qty_pcs
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;

  -- The row may also be a parent whose quantity, part or customer changed.
  IF TG_OP = 'UPDATE' AND EXISTS (
       SELECT 1 FROM trace.lot c JOIN trace.customer_order co ON co.order_id = c.order_id
        WHERE c.parent_lot_id = cur.lot_id
          AND (c.part_id <> cur.part_id OR co.customer_id <> cur.customer_id)) THEN
    RAISE EXCEPTION 'Child lots of % must keep its part and customer', cur.lot_no
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND (SELECT sum(qty_pcs) FROM trace.lot WHERE parent_lot_id = cur.lot_id) > cur.qty_pcs THEN
    RAISE EXCEPTION 'Child lots of % hold more pieces than the parent lot (% pcs)', cur.lot_no, cur.qty_pcs
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER lot_genealogy AFTER INSERT OR UPDATE OF parent_lot_id, qty_pcs, part_id, order_id ON trace.lot
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trace.check_lot_genealogy();

CREATE FUNCTION trace.check_step_within_load() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE bad record;
BEGIN
  IF TG_TABLE_NAME = 'load' THEN
    SELECT ld.load_id, ld.carrier_no, st.step_id INTO bad
      FROM trace.load ld JOIN trace.load_step st ON st.load_id = ld.load_id
     WHERE ld.load_id = NEW.load_id
       AND NOT st.in_station <@ tstzrange(ld.started_at, ld.finished_at, '[]')
     LIMIT 1;
  ELSE
    SELECT ld.load_id, ld.carrier_no, st.step_id INTO bad
      FROM trace.load ld JOIN trace.load_step st ON st.load_id = ld.load_id
     WHERE st.step_id = NEW.step_id
       AND NOT st.in_station <@ tstzrange(ld.started_at, ld.finished_at, '[]');
  END IF;
  IF FOUND THEN
    RAISE EXCEPTION 'Step % lies outside the run of carrier % (load %)', bad.step_id, bad.carrier_no, bad.load_id
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER load_step_within_load AFTER INSERT OR UPDATE OF load_id, in_station ON trace.load_step
  DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION trace.check_step_within_load();
CREATE CONSTRAINT TRIGGER load_times_cover_steps AFTER UPDATE OF started_at, finished_at ON trace.load
  DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION trace.check_step_within_load();
