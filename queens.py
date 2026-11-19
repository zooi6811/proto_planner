import tkinter as tk
from tkinter import messagebox
import random

COLORS = [
    "#FFB3BA", "#FFDFBA", "#FFFFBA", "#BAFFC9",
    "#BAE1FF", "#E0BBE4", "#FFC6FF", "#B5EAD7"
]

class RegionQueensPuzzle:
    def __init__(self, master):
        self.master = master
        self.master.title("Region Queens Puzzle")
        self.board_size = 8
        self.cell_size = 60
        
        self.grid_state = {} # (r, c) -> 'Q' or 'X'
        self.regions = []
        self.solution = []
        
        rules_text = (
            "Rules: 1 Queen per row, column, and color region.\n"
            "Queens cannot touch each other (not even diagonally).\n"
            "Left-Click: Place/Remove Queen | Right-Click: Place/Remove ✖"
        )
        tk.Label(self.master, text=rules_text, font=("Arial", 11, "bold"), justify=tk.CENTER).pack(pady=5)
        
        self.canvas = tk.Canvas(self.master, width=self.board_size * self.cell_size, height=self.board_size * self.cell_size)
        self.canvas.pack(padx=20, pady=5)
        
        self.canvas.bind("<Button-1>", self.on_left_click)
        self.canvas.bind("<Button-2>", self.on_right_click) # Mac right-click
        self.canvas.bind("<Button-3>", self.on_right_click) # Windows/Linux right-click
        
        btn_frame = tk.Frame(self.master)
        btn_frame.pack(pady=10)
        
        tk.Button(btn_frame, text="New Puzzle", font=("Arial", 12), command=self.new_game).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="Clear Board", font=("Arial", 12), command=self.clear_board).pack(side=tk.LEFT, padx=10)
        
        self.new_game()

    def generate_board(self, size=8):
        # 1. Generate a valid placement of queens (no two touch, 1 per row/col)
        cols = list(range(size))
        while True:
            random.shuffle(cols)
            valid = True
            for i in range(size - 1):
                if abs(cols[i] - cols[i+1]) <= 1:
                    valid = False
                    break
            if valid:
                break
                
        queens = [(r, cols[r]) for r in range(size)]
        
        # 2. Assign initial regions based on queen positions
        regions = [[-1 for _ in range(size)] for _ in range(size)]
        for i, (r, c) in enumerate(queens):
            regions[r][c] = i
            
        # 3. Randomly grow regions
        frontiers = {i: [] for i in range(size)}
        for i, (r, c) in enumerate(queens):
            for dr, dc in [(-1,0), (1,0), (0,-1), (0,1)]:
                nr, nc = r+dr, c+dc
                if 0 <= nr < size and 0 <= nc < size:
                    frontiers[i].append((nr, nc))
                    
        unassigned = size * size - size
        while unassigned > 0:
            active_regions = [i for i in range(size) if frontiers[i]]
            if not active_regions:
                break
            
            region = random.choice(active_regions)
            random.shuffle(frontiers[region])
            
            assigned_one = False
            for fr, fc in frontiers[region]:
                if regions[fr][fc] == -1:
                    regions[fr][fc] = region
                    unassigned -= 1
                    assigned_one = True
                    for dr, dc in [(-1,0), (1,0), (0,-1), (0,1)]:
                        nr, nc = fr+dr, fc+dc
                        if 0 <= nr < size and 0 <= nc < size and regions[nr][nc] == -1:
                            frontiers[region].append((nr, nc))
                    break
            
            if not assigned_one:
                frontiers[region] = [cell for cell in frontiers[region] if regions[cell[0]][cell[1]] == -1]

        # Cleanup isolated cells
        while any(-1 in row for row in regions):
            for r in range(size):
                for c in range(size):
                    if regions[r][c] == -1:
                        for dr, dc in [(-1,0), (1,0), (0,-1), (0,1)]:
                            nr, nc = r+dr, c+dc
                            if 0 <= nr < size and 0 <= nc < size and regions[nr][nc] != -1:
                                regions[r][c] = regions[nr][nc]
                                break

        return regions, queens

    def new_game(self):
        self.regions, self.solution = self.generate_board(self.board_size)
        self.clear_board()

    def clear_board(self):
        self.grid_state.clear()
        self.draw_board()

    def draw_board(self):
        self.canvas.delete("all")
        
        # Draw cells
        for r in range(self.board_size):
            for c in range(self.board_size):
                color = COLORS[self.regions[r][c] % len(COLORS)]
                x1, y1 = c * self.cell_size, r * self.cell_size
                x2, y2 = x1 + self.cell_size, y1 + self.cell_size
                self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="#dddddd")
                
                state = self.grid_state.get((r, c))
                if state == 'Q':
                    self.canvas.create_text(x1 + self.cell_size//2, y1 + self.cell_size//2, text="♛", font=("Arial", 32), fill="black")
                elif state == 'X':
                    self.canvas.create_text(x1 + self.cell_size//2, y1 + self.cell_size//2, text="✖", font=("Arial", 20), fill="#777777")

        # Draw region boundaries
        for r in range(self.board_size):
            for c in range(self.board_size):
                x1, y1 = c * self.cell_size, r * self.cell_size
                x2, y2 = x1 + self.cell_size, y1 + self.cell_size
                reg = self.regions[r][c]
                
                # Top
                if r == 0 or self.regions[r-1][c] != reg:
                    self.canvas.create_line(x1, y1, x2, y1, fill="black", width=3)
                # Bottom
                if r == self.board_size - 1 or self.regions[r+1][c] != reg:
                    self.canvas.create_line(x1, y2, x2, y2, fill="black", width=3)
                # Left
                if c == 0 or self.regions[r][c-1] != reg:
                    self.canvas.create_line(x1, y1, x1, y2, fill="black", width=3)
                # Right
                if c == self.board_size - 1 or self.regions[r][c+1] != reg:
                    self.canvas.create_line(x2, y1, x2, y2, fill="black", width=3)
                    
        # Highlight conflicts
        conflicts = self.get_conflicts()
        for r, c in conflicts:
            x1, y1 = c * self.cell_size, r * self.cell_size
            self.canvas.create_rectangle(x1, y1, x1 + self.cell_size, y1 + self.cell_size, outline="red", width=4)

    def get_conflicts(self):
        conflicts = set()
        queens = [pos for pos, state in self.grid_state.items() if state == 'Q']
        
        for i in range(len(queens)):
            for j in range(i + 1, len(queens)):
                r1, c1 = queens[i]
                r2, c2 = queens[j]
                
                is_conflict = False
                
                if r1 == r2 or c1 == c2:
                    is_conflict = True
                elif self.regions[r1][c1] == self.regions[r2][c2]:
                    is_conflict = True
                elif max(abs(r1 - r2), abs(c1 - c2)) <= 1: # Touching
                    is_conflict = True
                    
                if is_conflict:
                    conflicts.add(queens[i])
                    conflicts.add(queens[j])
                    
        return conflicts

    def on_left_click(self, event):
        self.handle_click(event, 'Q')

    def on_right_click(self, event):
        self.handle_click(event, 'X')

    def handle_click(self, event, target_state):
        col = event.x // self.cell_size
        row = event.y // self.cell_size
        
        if 0 <= row < self.board_size and 0 <= col < self.board_size:
            current = self.grid_state.get((row, col))
            if current == target_state:
                self.grid_state.pop((row, col), None)
            else:
                self.grid_state[(row, col)] = target_state
                
            self.draw_board()
            self.check_win()

    def check_win(self):
        queens = [pos for pos, state in self.grid_state.items() if state == 'Q']
        if len(queens) == self.board_size and not self.get_conflicts():
            self.master.after(50, lambda: messagebox.showinfo("Congratulations!", "You successfully solved the puzzle!"))

if __name__ == "__main__":
    root = tk.Tk()
    root.resizable(False, False)
    
    # Center window
    window_width = 8 * 60 + 40
    window_height = 8 * 60 + 130
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = int((screen_width/2) - (window_width/2))
    y = int((screen_height/2) - (window_height/2))
    root.geometry(f"{window_width}x{window_height}+{x}+{y}")
    
    app = RegionQueensPuzzle(root)
    root.mainloop()
