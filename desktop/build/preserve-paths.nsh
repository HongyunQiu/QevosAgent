!macro DefinePreservePaths Callback
  !insertmacro ${Callback} "resources\app\vendor\app\AGENTS.md"
  !insertmacro ${Callback} "resources\app\vendor\app\ADVISOR.md"
  !insertmacro ${Callback} "resources\app\vendor\app\SKILLS"
  !insertmacro ${Callback} "resources\app\vendor\app\runs"
  !insertmacro ${Callback} "resources\app\vendor\app\memory_episodic.jsonl"
  !insertmacro ${Callback} "resources\app\vendor\app\memory_macro.md"
  !insertmacro ${Callback} "resources\app\vendor\app\agent_tools.json"
!macroend

!macro DefineInstallOverwriteProtectedPaths Callback
  !insertmacro ${Callback} "resources\app\vendor\app\AGENTS.md"
  !insertmacro ${Callback} "resources\app\vendor\app\ADVISOR.md"
  !insertmacro ${Callback} "resources\app\vendor\app\SKILLS"
!macroend
