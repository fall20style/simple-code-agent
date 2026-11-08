import tkinter as tk
import random

class Tetris:
    def __init__(self, root):
        self.root = root
        self.root.title("Tetris")
        self.root.resizable(False, False)
        
        self.canvas = tk.Canvas(self.root, width=240, height=480, bg="black")
        self.canvas.pack()
        
        self.grid = [[0 for _ in range(10)] for _ in range(20)]
        self.score = 0
        
        self.create_grid()
        self.create_random_tile()
        self.draw_grid()
        
        self.root.bind("<Right>", self.move_right)
        self.root.bind("<Left>", self.move_left)
        self.root.bind("<Down>", self.move_down)
        self.root.bind("<Up>", self.rotate_tile)
        self.root.bind("<space>", self.drop_piece)
        
        self.game_loop()
    
    def create_grid(self):
        self.canvas.delete("all")
        for i in range(20):
            for j in range(10):
                if self.grid[i][j] == 1:
                    self.canvas.create_rectangle(j*24, i*24, j*24+24, i*24+24, fill="yellow", outline="black")
    
    def create_random_tile(self):
        shape = random.choice(["T", "O", "L", "J", "I", "S", "Z"])
        self.current_tile = self.create_tile(shape)
    
    def create_tile(self, shape):
        if shape == "T":
            return [[1, 1, 1], [0, 1, 0]]
        elif shape == "O":
            return [[1, 1], [1, 1]]
        elif shape == "L":
            return [[1, 1, 1], [1, 0, 0]]
        elif shape == "J":
            return [[1, 0, 0], [1, 1, 1]]
        elif shape == "I":
            return [[1, 1, 1, 1], [0, 0, 0, 0]]
        elif shape == "S":
            return [[0, 1, 1], [1, 1, 0]]
        elif shape == "Z":
            return [[1, 1, 0], [0, 1, 1]]
    
    def draw_grid(self):
        for i in range(20):
            for j in range(10):
                self.canvas.create_rectangle(j*24, i*24, j*24+24, i*24+24, fill="black", outline="black")
    
    def move_right(self, event=None):
        self.move_tile(right=True)
        self.draw_grid()
    
    def move_left(self, event=None):
        self.move_tile(right=False)
        self.draw_grid()
    
    def move_down(self, event=None):
        self.move_tile(down=True)
        self.draw_grid()
    
    def rotate_tile(self, event=None):
        self.rotate()
        self.draw_grid()
    
    def drop_piece(self, event=None):
        while self.can_move_down():
            self.move_down()
            self.check_line()
            self.draw_grid()
    
    def move_tile(self, right=True):
        if right:
            new_pos = [self.current_tile[i][j+1] for i, j in enumerate(self.current_tile)]
        else:
            new_pos = [self.current_tile[i][j-1] for i, j in enumerate(self.current_tile)]
        
        if self.is_valid(new_pos):
            self.current_tile = new_pos
            self.draw_grid()
    
    def is_valid(self, new_pos):
        for i, j in enumerate(new_pos):
            if j < 0 or j >= 10:
                return False
            if i >= 20:
                return False
            if self.grid[i][j] == 1:
                return False
        return True
    
    def check_line(self):
        for i in range(20):
            if all(self.grid[i][j] for j in range(10)):
                self.score += 1
                for j in range(10):
                    self.grid[i][j] = 0
                self.draw_grid()
    
    def rotate(self):
        new_tile = []
        for i, j in enumerate(self.current_tile):
            new_tile.append([j, i])
        self.current_tile = new_tile
    
    def game_loop(self):
        self.create_random_tile()
        self.draw_grid()
        self.root.after(1000, self.game_loop)
    
if __name__ == "__main__":
    root = tk.Tk()
    tetris = Tetris(root)
    root.mainloop()
