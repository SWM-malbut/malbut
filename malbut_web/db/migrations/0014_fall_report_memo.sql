-- Optional memo written with a missed-fall report (놓친 넘어짐 신고).
ALTER TABLE fall_incidents
  ADD COLUMN IF NOT EXISTS report_memo TEXT CHECK (report_memo IS NULL OR char_length(report_memo) <= 500);
