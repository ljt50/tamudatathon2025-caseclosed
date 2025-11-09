import os
from flask import Flask, request, jsonify
from threading import Lock
from collections import deque

from case_closed_game import Game, Direction, EMPTY, AGENT

app = Flask(__name__)

GLOBAL_GAME = Game()
LAST_POSTED_STATE = {}
game_lock = Lock()

PARTICIPANT = "ACPC_diddy_party_desuwa"
AGENT_NAME = "Sneaky_Golem"

# ---------------- Flood Fill Helper ----------------
def flood_fill_space(pos, board, occupied):
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

# ---------------- BFS helper for post-corridor filling ----------------
def bfs_next_move(head, occupied, board, preferred_dx):
    """Return the next safe move in post-corridor fill (prefers horizontal)."""
    w, h = board.width, board.height
    # First try to move horizontally in preferred direction
    nx, ny = (head[0] + preferred_dx) % w, head[1]
    if not is_suicidal(head, (nx, ny), board, occupied):
        return Direction.RIGHT if preferred_dx > 0 else Direction.LEFT
    # Otherwise try vertical moves
    for d in [Direction.UP, Direction.DOWN]:
        nx, ny = (head[0] + d.value[0]) % w, (head[1] + d.value[1]) % h
        if not is_suicidal(head, (nx, ny), board, occupied):
            return d
    # fallback: pick any safe non-reversing move
    for d in [Direction.LEFT, Direction.RIGHT, Direction.UP, Direction.DOWN]:
        nx, ny = (head[0] + d.value[0]) % w, (head[1] + d.value[1]) % h
        if not is_suicidal(head, (nx, ny), board, occupied):
            return d
    return None

# ---------------- Collision / suicide checker ----------------
def is_suicidal(head, target, board, occupied_trails):
    """
    Return True if moving from head -> target would collide with a trail/wall.
    Uses the board's authoritative cell state and explicit trail sets as a safety net.
    """
    tx, ty = target
    # Check with board API (source of truth)
    try:
        cell_state = board.get_cell_state((tx, ty))
    except Exception:
        # Fallback if board.get_cell_state isn't available
        cell_state = AGENT if (tx, ty) in occupied_trails else EMPTY

    if cell_state != EMPTY:
        return True

    # Double-check against known trails (defensive)
    if (tx, ty) in occupied_trails:
        return True

    return False

# ---------------- Suicide Move Selector ----------------
def choose_non_suicidal_move(head, board, occupied_trails, candidate_moves, cur_dir):
    """
    candidate_moves: list of (space, direction, (nx,ny))
    cur_dir: current direction tuple used to avoid reversing
    Return a chosen Direction object (guarantees non-suicidal if one exists).
    If none exist, return best candidate (most space) to match prior behavior.
    """
    if not candidate_moves:
        return None

    # Sort by space descending (prefer larger reachable area)
    candidate_moves.sort(reverse=True, key=lambda x: x[0])

    # Try to pick first non-suicidal
    for space, d, (nx, ny) in candidate_moves:
        if not is_suicidal(head, (nx, ny), board, occupied_trails):
            return d

    # no safe move -> return the best (most space) even if suicidal
    return candidate_moves[0][1]

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
    # Persistent flags for phase tracking
    if not hasattr(send_move, "phase"):
        send_move.phase = "floodfill"
        send_move.escape_dx = 0  # horizontal direction used during corridor escape

    player_number = request.args.get("player_number", default=1, type=int)
    with game_lock:
        my_agent = GLOBAL_GAME.agent1 if player_number == 1 else GLOBAL_GAME.agent2
        opponent = GLOBAL_GAME.agent2 if player_number == 1 else GLOBAL_GAME.agent1

    head = my_agent.trail[-1]
    opp_head = opponent.trail[-1] if opponent.alive else None
    w, h = GLOBAL_GAME.board.width, GLOBAL_GAME.board.height
    start_x, start_y = my_agent.trail[0]

    # ---------------- Sneaky corridor ----------------
    corridor_y = (start_y + h // 2) % h  # corridor on far side
    sneaky_corridor = {(x % w, corridor_y) for x in range(w)}
    exit_x = start_x  # horizontal exit toward original starting x

    # Occupied according to trails (defensive)
    occupied_trails = set(my_agent.trail) | set(opponent.trail)

    # ---------------- Safe moves ----------------
    directions = [d for d in Direction]
    cur_dx, cur_dy = my_agent.direction.value
    directions = [d for d in directions if d.value != (-cur_dx, -cur_dy)]  # avoid reversing

    move_options = []
    for d in directions:
        nx, ny = (head[0] + d.value[0]) % w, (head[1] + d.value[1]) % h
        # We'll still compute space for candidate moves but we do not filter by occupied here;
        # the suicide check uses the authoritative board state.
        # However skip if this coord is obviously our immediate trail head duplicate
        # (avoid choosing current head again)
        if (nx, ny) == head:
            continue
        space = flood_fill_space((nx, ny), GLOBAL_GAME.board, occupied_trails | sneaky_corridor)
        move_options.append((space, d, (nx, ny)))

    # If no moves at all, keep current direction (will likely die, but nothing else to do)
    if not move_options:
        chosen = my_agent.direction
        return jsonify({"move": chosen.name}), 200

    # ---------------- Phase transitions ----------------
    # Use BFS-ish metric? for now use BFS distance or x separation threshold depending on what you prefer;
    # keep original simple heuristic (horizontal separation) but you can substitute bfs_distance later.
    horiz_sep = abs(head[0] - opp_head[0]) if opp_head else float('inf')
    PANIC_THRESHOLD = 1

    # Panic trigger
    if send_move.phase == "floodfill" and horiz_sep <= PANIC_THRESHOLD:
        send_move.phase = "panic"

    # ---------------- Corridor Infiltration Check ----------------
    corridor_blocked = False
    if opp_head and send_move.phase == "panic":
        # If opponent is in the corridor row and between head x and exit_x then corridor is infiltrated
        if head[1] != corridor_y and opp_head[1] == corridor_y:
            if (head[0] < exit_x and head[0] < opp_head[0] <= exit_x) or \
               (head[0] > exit_x and exit_x <= opp_head[0] < head[0]):
                corridor_blocked = True
    if corridor_blocked:
        send_move.phase = "post_corridor_fill"

    # ---------------- Decide move by phase (using non-suicidal chooser) ----------------
    if send_move.phase == "floodfill":
        chosen = choose_non_suicidal_move(head, GLOBAL_GAME.board, occupied_trails, move_options, my_agent.direction)

    elif send_move.phase == "panic":
        # Step 1: Move vertically toward corridor_y if not aligned
        if head[1] != corridor_y:
            tentative = Direction.DOWN if (head[1] < corridor_y) else Direction.UP
        else:
            # Step 2: Move horizontally toward exit_x
            if head[0] != exit_x:
                tentative = Direction.RIGHT if head[0] < exit_x else Direction.LEFT
                send_move.escape_dx = 1 if head[0] < exit_x else -1
            else:
                # Step 3: Arrived at corridor exit
                send_move.phase = "post_corridor_fill"
                tentative = None

        # If tentative exists, ensure it's not suicidal by checking board
        if tentative is not None:
            nx, ny = (head[0] + tentative.value[0]) % w, (head[1] + tentative.value[1]) % h
            if not is_suicidal(head, (nx, ny), GLOBAL_GAME.board, occupied_trails):
                chosen = tentative
            else:
                # fallback to non-suicidal candidate
                chosen = choose_non_suicidal_move(head, GLOBAL_GAME.board, occupied_trails, move_options, my_agent.direction)
        else:
            chosen = choose_non_suicidal_move(head, GLOBAL_GAME.board, occupied_trails, move_options, my_agent.direction)

    elif send_move.phase == "post_corridor_fill":
        next_move = bfs_next_move(head, occupied_trails, GLOBAL_GAME.board, send_move.escape_dx or 1)
        if next_move is not None:
            nx, ny = (head[0] + next_move.value[0]) % w, (head[1] + next_move.value[1]) % h
            # double-check against authoritative board
            if not is_suicidal(head, (nx, ny), GLOBAL_GAME.board, occupied_trails):
                chosen = next_move
            else:
                chosen = choose_non_suicidal_move(head, GLOBAL_GAME.board, occupied_trails, move_options, my_agent.direction)
        else:
            chosen = choose_non_suicidal_move(head, GLOBAL_GAME.board, occupied_trails, move_options, my_agent.direction)

    # Final guard: if chosen somehow None, pick best candidate
    if chosen is None:
        chosen = move_options[0][1]

    return jsonify({"move": chosen.name}), 200

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

    # Reset for next game
    send_move.phase = "floodfill"
    send_move.escape_dx = 0

    return jsonify({"status": "acknowledged"}), 200

# ---------------- Main ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5008"))
    print(f"Starting {AGENT_NAME} ({PARTICIPANT}) on port {port}...")
    # disable reloader to avoid duplicate processes binding same port
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
