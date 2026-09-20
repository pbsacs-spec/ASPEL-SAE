Set oShell = CreateObject("WScript.Shell")
sBase    = "C:\Users\iscbruno\aspel-inventario"
sNode    = """C:\Program Files\nodejs\node.exe"""
sFlaskLog = sBase & "\flask.log"
sWaLog    = sBase & "\wa_service\wa.log"

' Iniciar Flask (oculto, log en flask.log)
oShell.Run "cmd /c cd /d """ & sBase & """ && py app.py >> """ & sFlaskLog & """ 2>&1", 0, False

' Esperar 3 segundos a que Flask levante
WScript.Sleep 3000

' Iniciar servicio WhatsApp (oculto, log en wa_service\wa.log)
oShell.Run "cmd /c cd /d """ & sBase & "\wa_service"" && " & sNode & " wa_service.js >> """ & sWaLog & """ 2>&1", 0, False

MsgBox "Servicios Aspel SAE iniciados correctamente." & Chr(13) & Chr(10) & Chr(13) & Chr(10) & _
       "Flask:     http://localhost:5000" & Chr(13) & Chr(10) & _
       "WhatsApp:  http://localhost:5000/admin/whatsapp" & Chr(13) & Chr(10) & Chr(13) & Chr(10) & _
       "Los servicios corren en segundo plano." & Chr(13) & Chr(10) & _
       "Para detenerlos usa: detener_servicios.bat", _
       64, "Aspel SAE"
