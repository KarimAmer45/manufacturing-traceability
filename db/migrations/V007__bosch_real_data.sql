-- V007 real public traceability data: Kaggle "Bosch Production Line Performance"
-- 1.18 M real parts, 4 lines, 51 stations, anonymised. Loaded by ingest/p1_load_bosch.py
CREATE TABLE bosch.part (
  part_id  bigint   PRIMARY KEY,                     -- Kaggle "Id"
  response smallint NOT NULL CHECK (response IN (0, 1))   -- 1 = failed internal QC
);

CREATE TABLE bosch.station (
  station_code text     PRIMARY KEY,               -- 'L3_S32'
  line_no      smallint NOT NULL,
  station_no   smallint NOT NULL
);

-- One row per part and station: first and last timestamp seen at that station.
-- Times are anonymised ticks, not dates. See the project 1 file for the tick to day finding.
CREATE TABLE bosch.station_pass (
  part_id      bigint       NOT NULL REFERENCES bosch.part,
  station_code text         NOT NULL REFERENCES bosch.station,
  t_first      numeric(8,2) NOT NULL,
  t_last       numeric(8,2) NOT NULL,
  PRIMARY KEY (part_id, station_code),
  CHECK (t_last >= t_first)
);
-- Add after the bulk load, then compare EXPLAIN ANALYZE before and after:
-- CREATE INDEX station_pass_station_time_idx ON bosch.station_pass (station_code, t_first);
