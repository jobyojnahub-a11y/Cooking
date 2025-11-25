import threading
import asyncio
from flask import Flask, render_template, request, redirect, url_for, flash

from bot import (
    config,
    APIHandler,
)

app = Flask(__name__)
app.secret_key = "super-secret-key-change-this"

def run_bot_polling():
    import bot
    bot.main()  # Starts Telegram bot polling and scheduling

@app.before_first_request
def startup():
    t = threading.Thread(target=run_bot_polling, daemon=True)
    t.start()

@app.route("/", methods=["GET"])
def index():
    batches = config.get_all_batches()
    return render_template("index.html", batches=batches)

@app.route("/add-batch", methods=["POST"])
def add_batch():
    batch_id = request.form.get("batch_id", "").strip()
    token = request.form.get("token", "").strip()
    channel_id = request.form.get("channel_id", "").strip()
    if not batch_id or not token or not channel_id:
        flash("All fields required.", "error")
        return redirect(url_for("index"))
    try: channel_id_int = int(channel_id)
    except: flash("Channel ID must be integer.", "error"); return redirect(url_for("index"))
    details = APIHandler.get_batch_details(batch_id, token)
    if not details or not details.get("success"):
        flash("Invalid batch or token.", "error"); return redirect(url_for("index"))
    batch_data = details.get("data",{}); name = batch_data.get("name", batch_id)
    config.add_batch(batch_id, token, channel_id_int, name)
    flash("Batch added successfully.", "success")
    return redirect(url_for("index"))

@app.route("/delete-batch/<batch_id>", methods=["POST"])
def delete_batch(batch_id):
    config.delete_batch(batch_id)
    flash("Batch deleted.", "success")
    return redirect(url_for("index"))

@app.route("/toggle-batch/<batch_id>", methods=["POST"])
def toggle_batch(batch_id):
    config.toggle_active(batch_id)
    flash("Batch status toggled.", "success")
    return redirect(url_for("index"))

@app.route("/update-token/<batch_id>", methods=["POST"])
def update_token(batch_id):
    token = request.form.get("new_token", "").strip()
    if not token:
        flash("Token required.", "error")
        return redirect(url_for("index"))
    details = APIHandler.get_batch_details(batch_id, token)
    if not details or not details.get("success"):
        flash("Invalid token for this batch.", "error")
        return redirect(url_for("index"))
    config.update_token(batch_id, token)
    flash("Token updated.", "success")
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
