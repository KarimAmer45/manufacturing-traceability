-- V013 the delivery-recipient check (V011) reads the rows as they are when it runs.
-- With SET CONSTRAINTS ... DEFERRED the check runs at commit; a wrong delivery_lot row that was
-- inserted and deleted again in the same transaction must not fail the commit, and an order whose
-- customer was changed twice must be checked with its final customer. Every branch therefore joins
-- the current rows instead of using the queued NEW values.
CREATE OR REPLACE FUNCTION trace.check_delivery_customer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE bad record;
BEGIN
  IF TG_TABLE_NAME = 'delivery_lot' THEN
    SELECT d.delivery_no, l.lot_no INTO bad
      FROM trace.delivery_lot dl JOIN trace.delivery d ON d.delivery_id = dl.delivery_id
      JOIN trace.lot l ON l.lot_id = dl.lot_id JOIN trace.customer_order o ON o.order_id = l.order_id
     WHERE dl.delivery_id = NEW.delivery_id AND dl.lot_id = NEW.lot_id AND d.customer_id <> o.customer_id;
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
      FROM trace.customer_order o JOIN trace.lot l ON l.order_id = o.order_id
      JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
      JOIN trace.delivery d ON d.delivery_id = dl.delivery_id
     WHERE o.order_id = NEW.order_id AND d.customer_id <> o.customer_id LIMIT 1;
  END IF;
  IF FOUND THEN
    RAISE EXCEPTION 'Delivery % for lot % is addressed to a different customer than the order', bad.delivery_no, bad.lot_no
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END $$;
