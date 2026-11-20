# Typing Debt in core/

This document records typing debt identified by mypy core/ --config-file=mypy.ini --no-site-packages as part of Phase 2 quality gates.

## Summary

Found 157 errors in 12 files (checked 54 source files)

## Errors by Module

### core\_smoke_harness.py (9 issues)

`	ext
core\_smoke_harness.py:38: error: Need type annotation for "report"  [var-annotated]
core\_smoke_harness.py:95: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
core\_smoke_harness.py:143: error: Need type annotation for "res"  [var-annotated]
core\_smoke_harness.py:150: error: Argument 1 to "module_from_spec" has incompatible type "ModuleSpec | None"; expected "ModuleSpec"  [arg-type]
core\_smoke_harness.py:152: error: Item "None" of "ModuleSpec | None" has no attribute "loader"  [union-attr]
core\_smoke_harness.py:152: error: Item "None" of "Loader | Any | None" has no attribute "exec_module"  [union-attr]
core\_smoke_harness.py:186: error: Incompatible types in assignment (expression has type "float", target has type "int | list[Any] | str | None")  [assignment]
core\_smoke_harness.py:189: error: Incompatible types in assignment (expression has type "dict[Any, Any]", target has type "int | list[Any] | str | None")  [assignment]
core\_smoke_harness.py:200: error: Item "None" of "TextIOWrapper[_WrappedBuffer] | None" has no attribute "flush"  [union-attr]
`

### core\action_loader.py (1 issues)

`	ext
core\action_loader.py:156: error: Incompatible return value type (got "str", expected "ActionResult")  [return-value]
`

### core\agent_orchestrator.py (2 issues)

`	ext
core\agent_orchestrator.py:74: error: Unexpected keyword argument "images" for "text"  [call-arg]
core\agent_orchestrator.py:74: note: "text" defined in "core.gemini"
`

### core\audio_manager.py (1 issues)

`	ext
core\audio_manager.py:541: error: Argument 2 to "resample_pcm_int16" has incompatible type "int | None"; expected "int"  [arg-type]
`

### core\live_session.py (6 issues)

`	ext
core\live_session.py:91: error: Incompatible types in assignment (expression has type "float | None", base class "LiveVoiceMixin" defined the type as "float")  [assignment]
core\live_session.py:193: error: Incompatible types in assignment (expression has type "WakeWordDetector | None", base class "LiveVoiceMixin" defined the type as "WakeWordDetector")  [assignment]
core\live_session.py:232: error: Argument "on_toggle_mute" to "GlobalInputController" has incompatible type "Callable[[], VoiceState]"; expected "Callable[[], None] | None"  [arg-type]
core\live_session.py:233: error: Argument "on_force_listening" to "GlobalInputController" has incompatible type "Callable[[], VoiceState]"; expected "Callable[[], None] | None"  [arg-type]
core\live_session.py:894: error: Item "None" of "WakeWordDetector | None" has no attribute "stop"  [union-attr]
core\live_session.py:899: error: Item "None" of "PushToTalk | None" has no attribute "stop"  [union-attr]
`

### core\session_audio.py (11 issues)

`	ext
core\session_audio.py:27: error: "LiveAudioMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_audio.py:34: error: "LiveAudioMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_audio.py:126: error: Cannot determine type of "_pending_vision"  [has-type]
core\session_audio.py:128: error: Cannot determine type of "_pending_vision"  [has-type]
core\session_audio.py:131: error: "LiveAudioMixin" has no attribute "ui"  [attr-defined]
core\session_audio.py:132: error: "LiveAudioMixin" has no attribute "ui"  [attr-defined]
core\session_audio.py:137: error: "LiveAudioMixin" has no attribute "session"  [attr-defined]
core\session_audio.py:142: error: "LiveAudioMixin" has no attribute "session"  [attr-defined]
core\session_audio.py:146: error: "LiveAudioMixin" has no attribute "_arm_stall"  [attr-defined]
core\session_audio.py:156: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
core\session_audio.py:157: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
`

### core\session_monitors.py (42 issues)

`	ext
core\session_monitors.py:43: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:57: error: "LiveMonitorsMixin" has no attribute "_turn_done_event"  [attr-defined]
core\session_monitors.py:58: error: "LiveMonitorsMixin" has no attribute "_turn_done_event"  [attr-defined]
core\session_monitors.py:60: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:67: error: Cannot determine type of "_session_log"  [has-type]
core\session_monitors.py:70: error: Need type annotation for "_session_log" (hint: "_session_log: list[<type>] = ...")  [var-annotated]
core\session_monitors.py:103: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:104: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:106: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:107: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:109: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:110: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:112: error: "LiveMonitorsMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_monitors.py:113: error: "LiveMonitorsMixin" has no attribute "_is_speaking"  [attr-defined]
core\session_monitors.py:114: error: "LiveMonitorsMixin" has no attribute "_last_user_speech"  [attr-defined]
core\session_monitors.py:117: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:123: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:128: error: "LiveMonitorsMixin" has no attribute "speak"  [attr-defined]
core\session_monitors.py:136: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:137: error: "LiveMonitorsMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_monitors.py:138: error: "LiveMonitorsMixin" has no attribute "_is_speaking"  [attr-defined]
core\session_monitors.py:139: error: "LiveMonitorsMixin" has no attribute "_last_user_speech"  [attr-defined]
core\session_monitors.py:150: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:163: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:166: error: "LiveMonitorsMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_monitors.py:167: error: "LiveMonitorsMixin" has no attribute "_is_speaking"  [attr-defined]
core\session_monitors.py:174: error: "LiveMonitorsMixin" has no attribute "_last_user_speech"  [attr-defined]
core\session_monitors.py:189: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:198: error: "LiveMonitorsMixin" has no attribute "_dashboard"  [attr-defined]
core\session_monitors.py:206: error: "LiveMonitorsMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_monitors.py:207: error: "LiveMonitorsMixin" has no attribute "_is_speaking"  [attr-defined]
core\session_monitors.py:208: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:210: error: "LiveMonitorsMixin" has no attribute "out_queue"  [attr-defined]
core\session_monitors.py:215: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:216: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
core\session_monitors.py:222: error: "LiveMonitorsMixin" has no attribute "_dashboard"  [attr-defined]
core\session_monitors.py:227: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:230: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:232: error: "LiveMonitorsMixin" has no attribute "wake"; maybe "_awake"?  [attr-defined]
core\session_monitors.py:233: error: "LiveMonitorsMixin" has no attribute "_record_activity"  [attr-defined]
core\session_monitors.py:234: error: "LiveMonitorsMixin" has no attribute "session"  [attr-defined]
core\session_monitors.py:238: error: "LiveMonitorsMixin" has no attribute "ui"  [attr-defined]
`

### core\session_voice.py (79 issues)

`	ext
core\session_voice.py:31: error: Cannot determine type of "_out_latency"  [has-type]
core\session_voice.py:34: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:35: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:36: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:37: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:87: error: "LiveVoiceMixin" has no attribute "_wake_words"; maybe "_wake_words_label"?  [attr-defined]
core\session_voice.py:88: error: "LiveVoiceMixin" has no attribute "_wake_threshold"  [attr-defined]
core\session_voice.py:95: error: "LiveVoiceMixin" has no attribute "_wake_detector_lock"; maybe "_wake_detector"?  [attr-defined]
core\session_voice.py:96: error: Cannot determine type of "_wake_detector"  [has-type]
core\session_voice.py:99: error: "LiveVoiceMixin" has no attribute "_wake_threshold"  [attr-defined]
core\session_voice.py:100: error: "LiveVoiceMixin" has no attribute "_wake_words"; maybe "_wake_words_label"?  [attr-defined]
core\session_voice.py:102: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:135: error: Cannot determine type of "_awake"  [has-type]
core\session_voice.py:138: error: Cannot determine type of "_awake"  [has-type]
core\session_voice.py:142: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:143: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:144: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:162: error: "LiveVoiceMixin" has no attribute "_wake_detector_lock"; maybe "_wake_detector"?  [attr-defined]
core\session_voice.py:170: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:171: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:179: error: "LiveVoiceMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_voice.py:208: error: "LiveVoiceMixin" has no attribute "_wake_detector_lock"; maybe "_wake_detector"?  [attr-defined]
core\session_voice.py:214: error: Incompatible types in assignment (expression has type "None", variable has type "WakeWordDetector")  [assignment]
core\session_voice.py:224: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:227: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:249: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:258: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:338: error: "LiveVoiceMixin" has no attribute "session"  [attr-defined]
core\session_voice.py:365: error: Cannot determine type of "_rx_deadline"  [has-type]
core\session_voice.py:370: error: Incompatible types in assignment (expression has type "None", variable has type "float")  [assignment]
core\session_voice.py:387: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:400: error: "LiveVoiceMixin" has no attribute "_reconnect_event"; maybe "_reconnect_keep" or "_reconnect_reason"?  [attr-defined]
core\session_voice.py:560: error: "LiveVoiceMixin" has no attribute "_loop"  [attr-defined]
core\session_voice.py:560: error: "LiveVoiceMixin" has no attribute "session"  [attr-defined]
core\session_voice.py:569: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:576: error: "LiveVoiceMixin" has no attribute "session"  [attr-defined]
core\session_voice.py:580: error: "LiveVoiceMixin" has no attribute "_loop"  [attr-defined]
core\session_voice.py:588: error: "LiveVoiceMixin" has no attribute "_speaking_lock"  [attr-defined]
core\session_voice.py:609: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:611: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:618: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:627: error: Cannot determine type of "_ptt"  [has-type]
core\session_voice.py:628: error: Cannot determine type of "_ptt"  [has-type]
core\session_voice.py:639: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:664: error: "LiveVoiceMixin" has no attribute "_engine"  [attr-defined]
core\session_voice.py:664: error: "LiveVoiceMixin" has no attribute "_loop"  [attr-defined]
core\session_voice.py:667: error: "LiveVoiceMixin" has no attribute "_engine"  [attr-defined]
core\session_voice.py:667: error: "LiveVoiceMixin" has no attribute "_loop"  [attr-defined]
core\session_voice.py:682: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:688: error: "LiveVoiceMixin" has no attribute "_interrupt_lock"  [attr-defined]
core\session_voice.py:689: error: Cannot determine type of "_in_interrupt"  [has-type]
core\session_voice.py:697: error: "LiveVoiceMixin" has no attribute "audio_in_queue"  [attr-defined]
core\session_voice.py:719: error: "LiveVoiceMixin" has no attribute "_visemes"  [attr-defined]
core\session_voice.py:721: error: "LiveVoiceMixin" has no attribute "_turn_done_event"  [attr-defined]
core\session_voice.py:722: error: "LiveVoiceMixin" has no attribute "_turn_done_event"  [attr-defined]
core\session_voice.py:731: error: "LiveVoiceMixin" has no attribute "_engine"  [attr-defined]
core\session_voice.py:733: error: "LiveVoiceMixin" has no attribute "_engine"  [attr-defined]
core\session_voice.py:738: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:740: error: "LiveVoiceMixin" has no attribute "_interrupt_lock"  [attr-defined]
core\session_voice.py:750: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:758: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:759: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:761: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:768: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:776: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:780: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:797: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:800: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:801: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:809: error: "LiveVoiceMixin" has no attribute "voice_state"  [attr-defined]
core\session_voice.py:815: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:817: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:819: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:821: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
core\session_voice.py:873: error: "LiveVoiceMixin" has no attribute "_loop"  [attr-defined]
core\session_voice.py:873: error: "LiveVoiceMixin" has no attribute "session"  [attr-defined]
core\session_voice.py:876: error: "LiveVoiceMixin" has no attribute "session"  [attr-defined]
core\session_voice.py:880: error: "LiveVoiceMixin" has no attribute "_loop"  [attr-defined]
core\session_voice.py:886: error: "LiveVoiceMixin" has no attribute "ui"  [attr-defined]
`

### core\skill_learning\learning_manager.py (2 issues)

`	ext
core\skill_learning\learning_manager.py:69: error: Item "None" of "LearningSession | None" has no attribute "goal"  [union-attr]
core\skill_learning\learning_manager.py:145: error: Argument 1 to "approve_skill" of "LearningManager" has incompatible type "Any | None"; expected "str"  [arg-type]
`

### core\skill_learning\skill_executor.py (2 issues)

`	ext
core\skill_learning\skill_executor.py:310: error: Incompatible types in assignment (expression has type "dict[Any, Any]", target has type "str")  [assignment]
core\skill_learning\skill_executor.py:312: error: Incompatible types in assignment (expression has type "list[str | Any]", target has type "str")  [assignment]
`

### core\skill_learning\skill_registry.py (1 issues)

`	ext
core\skill_learning\skill_registry.py:28: note: By default the bodies of untyped functions are not checked, consider using --check-untyped-defs  [annotation-unchecked]
`

### core\skill_learning\skill_storage.py (2 issues)

`	ext
core\skill_learning\skill_storage.py:122: error: Unsupported target for indexed assignment ("Collection[str]")  [index]
core\skill_learning\skill_storage.py:146: error: Unsupported target for indexed assignment ("Collection[str]")  [index]
`

### core\skill_learning\skill_validator.py (4 issues)

`	ext
core\skill_learning\skill_validator.py:116: error: "object" has no attribute "append"  [attr-defined]
core\skill_learning\skill_validator.py:139: error: "object" has no attribute "append"  [attr-defined]
core\skill_learning\skill_validator.py:144: error: "object" has no attribute "append"  [attr-defined]
core\skill_learning\skill_validator.py:146: error: "object" has no attribute "append"  [attr-defined]
`
