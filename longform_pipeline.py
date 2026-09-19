import subprocess, sys
from pathlib import Path

def run(name):
    print("\n"+"="*72+"\n"+name+"\n"+"="*72)
    subprocess.run([sys.executable,name],check=True)

def main():
    Path("work/longform").mkdir(parents=True,exist_ok=True)
    run("kick_game_discovery.py")
    run("longform_ranker.py")
    run("longform_collect.py")
    run("longform_production.py")
    run("longform_upload.py")
    print("\nVIRALSPAWNTV LONG-FORM V1 COMPLETE")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM PIPELINE FAILED:",e); sys.exit(1)
