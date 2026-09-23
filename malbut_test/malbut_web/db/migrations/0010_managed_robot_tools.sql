-- Real-robot map deletion, Zones, manual steps and debugging requests.
ALTER TABLE robot_commands DROP CONSTRAINT IF EXISTS robot_commands_operation_check;
ALTER TABLE robot_commands ADD CONSTRAINT robot_commands_operation_check CHECK (operation IN (
  'start', 'finish', 'cancel',
  'navigation_preview', 'navigation_start', 'navigation_cancel',
  'drive_mode_start', 'drive_mode_pause', 'drive_mode_resume', 'drive_mode_stop',
  'room_split', 'room_merge', 'rooms_save', 'zones_apply',
  'demo_person_show', 'demo_person_hide',
  'runtime_start', 'runtime_stop', 'mission_start', 'mission_cancel',
  'map_delete', 'manual_move', 'zones_save',
  'robot_ping', 'robot_diagnostics', 'debug_mission_start'
));
