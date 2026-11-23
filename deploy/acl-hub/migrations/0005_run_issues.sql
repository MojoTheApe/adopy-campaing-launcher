-- «Опубликовано с проблемами»: ACL вернул success, но записал blocking_failures (например, расхождения при
-- перечитывании объявлений). В ACL такие кампании жёлтые. issues — сколько таких строк; у неуспешных 0.
ALTER TABLE runs ADD COLUMN issues INTEGER NOT NULL DEFAULT 0;

-- История: пересчитать по уже лежащим событиям publish.result (последнее событие прогона).
UPDATE runs SET issues = coalesce((
  SELECT json_array_length(json_extract(e.payload, '$.blocking_failures')) FROM events e
  WHERE e.type = 'publish.result' AND e.job_id = runs.job_id
    AND coalesce(e.campaign, json_extract(e.payload, '$.campaign_name')) = runs.campaign
  ORDER BY e.ts DESC LIMIT 1), 0)
WHERE outcome = 'ok';

UPDATE runs SET error = (
  SELECT json_extract(e.payload, '$.blocking_failures[0]') FROM events e
  WHERE e.type = 'publish.result' AND e.job_id = runs.job_id
    AND coalesce(e.campaign, json_extract(e.payload, '$.campaign_name')) = runs.campaign
  ORDER BY e.ts DESC LIMIT 1)
WHERE outcome = 'ok' AND issues > 0;
