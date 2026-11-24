from dotenv import load_dotenv
load_dotenv()
from dealbot.main import run_once
from dealbot.feed_exporter import export_feeds

if __name__ == "__main__":
    run_once()
    export_feeds()
