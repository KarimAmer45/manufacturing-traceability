-- Project 1 only. No dependency on tables belonging to later projects.
GRANT USAGE ON SCHEMA ref, lab, trace, bosch TO role_readonly, role_operator, role_lab, role_quality, role_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, lab, trace, bosch TO role_readonly, role_operator, role_lab, role_quality, role_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ref, lab, trace, bosch TO role_operator, role_lab, role_quality, role_ingest;
GRANT INSERT, UPDATE ON trace.load, trace.load_lot, trace.load_step TO role_operator;
GRANT INSERT ON trace.measurement TO role_operator;
GRANT INSERT ON lab.analysis TO role_lab;
GRANT INSERT, UPDATE ON lab.analysis_result TO role_lab;
GRANT INSERT, UPDATE ON lab.parameter_limit, trace.complaint TO role_quality;
GRANT UPDATE (status) ON trace.lot TO role_quality;
GRANT UPDATE (value, result_text) ON trace.measurement TO role_quality;
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ref, lab, trace, bosch TO role_ingest;
REVOKE ALL ON audit.change_log FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO role_quality;
GRANT SELECT ON audit.change_log TO role_quality;
-- The allowed analysis age is a quality decision (changes are audited).
GRANT INSERT, UPDATE ON lab.analysis_max_age TO role_quality;
-- The broad grants above must not undo the evidence hardening of V009: the analysis timeline is
-- maintained only by its triggers, and a limit version can be closed but not deleted.
REVOKE UPDATE, DELETE, TRUNCATE ON lab.analysis FROM role_lab, role_ingest, role_operator, role_quality;
REVOKE DELETE, TRUNCATE ON lab.parameter_limit FROM role_quality, role_ingest;
