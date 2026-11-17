# HA-paket som hor till BatteryControl

Filerna har ar kopior av det som ligger i `/config/packages/` pa HA-boxen.
De ar inte kalla - HA lases fran `/config`. Kopiorna finns for att andringar
ska ga att spara, granska och ataerga via git.

## batterycontrol_larm.yaml

Larm nar kvartsloopen slutar publicera. Skapad 2026-11-17.

**Kopiera ALDRIG upp den utan att kora check_config forst.** Ett trasigt
paket hindrar HA fran att starta om.

### Ladda upp

`/config` finns inte i klo-containern - anvand SFTP (paramiko, se TOOLS.md):

    sftp.open('/config/packages/batterycontrol_larm.yaml', 'wb').write(content)

Verifiera alltid efterat: `check_config` ska svara `valid`.

### Ladda om - alla fyra behovs

    input_datetime, input_boolean, template, automation

POST till `/api/services/<domän>/reload` med tom body. `homeassistant.reload`
racker inte - det laddar inte om template-sensorer.

### Entitets-ID:n ar namngivna av HA, inte av unique_id

Sensorn heter `binary_sensor.batterycontrol_kvartsloop_tyst` darfor att
namnet ar "BatteryControl kvartsloop tyst". `unique_id` styr bara registret.
Andras namnet flyttar ID:t och automationerna slutar fungera - de refererar
ID:t, inte namnet.

Samma sak for automationerna: `batterycontrol_larm_tyst_loop` (id) blev
entiteten `automation.batterycontrol_larm_kvartsloopen_star_still` (alias).

### Testa innan du litar pa den

En larmautomation som aldrig utlosts ar en gissning. Laggs en tillfallig
fil i `/config/packages/` med en boolean-styrd testsensor, kor reload,
satt booleansen - och kontrollera att `last_triggered` satts. En sensor som
redan ar `on` vid reload ger INGEN overgang, sa triggern `to: "on"` fyras
aldrig. Det misstaget kostade en felsokningsrunda.
