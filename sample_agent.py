import os
from flask import Flask, request, jsonify
from threading import Lock
from collections import deque

from case_closed_game import Game, Direction, GameResult

app = Flask(__name__)

GLOBAL_GAME = Game()
LAST_POSTED_STATE = {}
game_lock = Lock()

PARTICIPANT = "ParticipantX"
AGENT_NAME = "TrumpAgent"

# ---------------- Flood Fill Helper ----------------
def flood_fill_space(pos, board, occupied):
    """Returns the number of empty squares reachable from pos."""
    w, h = board.width, board.height
    visited = set()
    queue = deque([pos])
    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or (x, y) in occupied:
            continue
        visited.add((x, y))
        for dx, dy in [d.value for d in Direction]:
            nx, ny = (x + dx) % w, (y + dy) % h
            if (nx, ny) not in visited and (nx, ny) not in occupied:
                queue.append((nx, ny))
    return len(visited)

# ---------------- Update Local Game ----------------
def _update_local_game_from_post(data: dict):
    with game_lock:
        LAST_POSTED_STATE.clear()
        LAST_POSTED_STATE.update(data)
        if "board" in data:
            try:
                GLOBAL_GAME.board.grid = data["board"]
            except Exception:
                pass
        if "agent1_trail" in data:
            GLOBAL_GAME.agent1.trail = deque(tuple(p) for p in data["agent1_trail"])
        if "agent2_trail" in data:
            GLOBAL_GAME.agent2.trail = deque(tuple(p) for p in data["agent2_trail"])
        if "agent1_length" in data:
            GLOBAL_GAME.agent1.length = int(data["agent1_length"])
        if "agent2_length" in data:
            GLOBAL_GAME.agent2.length = int(data["agent2_length"])
        if "agent1_alive" in data:
            GLOBAL_GAME.agent1.alive = bool(data["agent1_alive"])
        if "agent2_alive" in data:
            GLOBAL_GAME.agent2.alive = bool(data["agent2_alive"])
        if "agent1_boosts" in data:
            GLOBAL_GAME.agent1.boosts_remaining = int(data["agent1_boosts"])
        if "agent2_boosts" in data:
            GLOBAL_GAME.agent2.boosts_remaining = int(data["agent2_boosts"])
        if "turn_count" in data:
            GLOBAL_GAME.turns = int(data["turn_count"])

# ---------------- Send Move ----------------
@app.route("/send-move", methods=["GET"])
def send_move():
    player_number = request.args.get("player_number", default=1, type=int)
    with game_lock:
        state = dict(LAST_POSTED_STATE)
        my_agent = GLOBAL_GAME.agent1 if player_number == 1 else GLOBAL_GAME.agent2
        opponent = GLOBAL_GAME.agent2 if player_number == 1 else GLOBAL_GAME.agent1
        boosts_remaining = my_agent.boosts_remaining
        turn_count = state.get("turn_count", 0)

    # Safe moves
    head = my_agent.trail[-1]
    opp_head = opponent.trail[-1] if opponent.alive else None
    directions = [d for d in Direction]
    cur_dx, cur_dy = my_agent.direction.value
    directions = [d for d in directions if d.value != (-cur_dx, -cur_dy)]

    safe_moves = []
    for d in directions:
        dx, dy = d.value
        nx, ny = (head[0] + dx) % GLOBAL_GAME.board.width, (head[1] + dy) % GLOBAL_GAME.board.height
        if (nx, ny) in my_agent.trail or (nx, ny) in opponent.trail:
            continue
        if opp_head:
            opp_next = [((opp_head[0] + odx) % GLOBAL_GAME.board.width,
                         (opp_head[1] + ody) % GLOBAL_GAME.board.height)
                        for odx, ody in [dd.value for dd in Direction]]
            if (nx, ny) in opp_next:
                continue
        safe_moves.append(d)

    if not safe_moves:
        chosen = my_agent.direction
    else:
        # Evaluate space for each safe move
        max_space = -1
        chosen = safe_moves[0]
        for d in safe_moves:
            dx, dy = d.value
            nx, ny = (head[0] + dx) % GLOBAL_GAME.board.width, (head[1] + dy) % GLOBAL_GAME.board.height
            occupied = set(my_agent.trail) | set(opponent.trail)
            space = flood_fill_space((nx, ny), GLOBAL_GAME.board, occupied)
            if space > max_space:
                max_space = space
                chosen = d

    # Use boost if available and space is large
    use_boost = boosts_remaining > 0 and max_space > 10
    move_str = f"{chosen.name}:BOOST" if use_boost else chosen.name
    return jsonify({"move": move_str}), 200

# ---------------- Boilerplate ----------------
@app.route("/", methods=["GET"])
def info():
    return jsonify({"participant": PARTICIPANT, "agent_name": AGENT_NAME}), 200

@app.route("/send-state", methods=["POST"])
def receive_state():
    data = request.get_json()
    if not data:
        return jsonify({"error": "no json body"}), 400
    _update_local_game_from_post(data)
    return jsonify({"status": "state received"}), 200

@app.route("/end", methods=["POST"])
def end_game():
    data = request.get_json()
    if data:
        _update_local_game_from_post(data)
    return jsonify({"status": "acknowledged"}), 200

# ---------------- Main ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5009"))
    print(f"Starting {AGENT_NAME} ({PARTICIPANT}) on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
