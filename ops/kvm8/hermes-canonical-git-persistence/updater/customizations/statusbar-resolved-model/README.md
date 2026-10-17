CUSTOMIZATION_ID=statusbar-resolved-model
CONTROL_PLANE_SOURCE=/opt/mastertrend-hermes/customizations/statusbar-resolved-model/

Arquivo atual de runtime:
- /opt/hermes-agent-clean/agent/turn_response_intake.py
- /opt/hermes-agent-clean/hermes_cli/cli_status_bar_mixin.py

Contrato funcional:
response.model -> agent._last_actual_response_model -> statusbar snapshot -> footer mostra modelo real
