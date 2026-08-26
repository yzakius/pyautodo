import os, socket, time
from datetime import datetime

from dotenv import load_dotenv as env
from pydo import Client
from azure.core.exceptions import ClientAuthenticationError

env()

token = os.getenv("DO_TOKEN")
print("Token carregado" if token else "Token não encontrado")

token = os.getenv("DO_TOKEN")
raw_skip = os.getenv("SKIP_LIST", "")
client = Client(token)

try:
    droplets_info = client.droplets.list()
except ClientAuthenticationError as e:
    print("Token inválido ou expirado")
    print(str(e))
    raise SystemExit(1)

droplets_list = droplets_info.get("droplets")
skip_list = [item.strip() for item in raw_skip.split(",") if item.strip()]
progress_snapshot = 0

def get_droplet_status(droplet_id):
    droplet = client.droplets.get(droplet_id=droplet_id)
    return droplet["droplet"]["status"]

def get_droplets_snapshot_list(droplet_id):
    print("Listando snapshots...")
    snapshots_list = client.droplets.list_snapshots(droplet_id)
    total = snapshots_list["meta"]["total"]
    id = None
    if total > 0:
        id = snapshots_list["snapshots"][0]["id"]
    return {"total": total, "id": id}

def delete_droplet_snapshot(snapshot_id, droplet_id):
    print(f"Preparando deleção de snapshot antigo...")
    snapshots = get_droplets_snapshot_list(droplet_id=droplet_id)
    print(f"O droplet possui {snapshots['total']} snapshots.")
    print("Deletando o snapshot antigo...")
    client.snapshots.delete(snapshot_id=snapshot_id)
    while True:
        snapshots = get_droplets_snapshot_list(droplet_id=droplet_id)
        time.sleep(18)
        if snapshots["total"] == 1:
            print("Snapshot deletado com sucesso!")
            print(f"O droplet possui agora {snapshots['total']} snapshots.")
            return

def create_droplet_snapshot(droplet_id, droplet_name):
    print("Criando snapshot...")
    time_stamp = datetime.now().strftime("%Y-%m")
    resp = client.droplet_actions.post(
        droplet_id,
        body={"type": "snapshot", "name": time_stamp + "-" + droplet_name}
    )
    action_id = resp["action"]["id"]
    while True:
        action = client.droplet_actions.get(droplet_id=droplet_id, action_id=action_id)
        status = action["action"]["status"]
        print(f"Criando o snapshot. Status: {status}")
        if status == "completed":
            print("Snapshot criado com sucesso!!")
            return True
        time.sleep(30)

def power_on_droplet(droplet_id, droplet_ip):
    print("Religando o Droplet...")
    client.droplet_actions.post(
        droplet_id=droplet_id,
        body={"type": "power_on"}
    )
    while True:
        try:
            s = socket.create_connection((droplet_ip, 22), timeout=5)
            print("Droplet ligado com sucesso!")
            s.close()
            break
        except Exception as e:
            print("Aguardando o droplet ligar...", e)
            time.sleep(20)


def power_off_droplet(droplet_id):
    print(f"Iniciando o desligamento do droplet.")
    client.droplet_actions.post(
        droplet_id=droplet_id,
        body={"type": "power_off"}
    )
    while True:
        droplet_status = get_droplet_status(droplet_id=droplet_id)
        if droplet_status != "off":
            print(f"{droplet_status}...")
            time.sleep(5)
        print(f"Droplet desligado com sucesso!")
        return

contagem = 0
for droplet in droplets_list:
    contagem += 1
    droplet_name = droplet.get("name")
    droplet_id = droplet.get("id")
    droplet_network = droplet["networks"]["v4"]
    print("=" * 45)
    print(f"Progresso: {contagem}/{len(droplets_list)}")
    print(f"Preparando o droplet {droplet_name}")
    if droplet_name in skip_list:
        print("Não será feito o snapshot.")
    else:
        for network in droplet["networks"]["v4"]:
            if network["type"] == "public":
                droplet_ip = network["ip_address"]

        snapshots = get_droplets_snapshot_list(droplet_id=droplet_id)

        if snapshots["total"] > 0:
            print(f"Encontrados {snapshots}")
            power_off_droplet(droplet_id=droplet_id)
            created = create_droplet_snapshot(droplet_id=droplet_id, droplet_name=droplet_name)
            if created:
                delete_droplet_snapshot(snapshot_id=snapshots["id"], droplet_id=droplet_id)
                print(droplet_id)
                print(droplet_ip)
                power_on_droplet(droplet_id=droplet_id, droplet_ip=droplet_ip)
        else:
            print(f"O droplet {droplet_name} não irá fazer snapshot.")
    print("=" * 45)
