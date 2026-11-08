import tkinter as tk
from tkinter import filedialog
import os

class TreeGameApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Tree Game")
        self.canvas = tk.Canvas(self.root, width=400, height=400)
        self.canvas.pack()
        self.tree = tk.Menu(self.root)
        self.root.config(menu=self.tree)
        self.file_menu = tk.Menu(self.tree)
        self.tree.add_cascade(label="File", menu=self.file_menu)
        self.file_menu.add_command(label="New", command=self.new_game)
        self.file_menu.add_command(label="Open", command=self.open_game)
        self.file_menu.add_command(label="Save", command=self.save_game)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.root.quit)

        self.canvas.bind("<Button-1>" , self.on_click)
        self.selected_node = None
        self.nodes = []
        self.current_node = None
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def new_game(self):
        self.canvas.delete("all")
        self.nodes = []
        self.current_node = None
        self.selected_node = None
        self.create_root_node()

    def create_root_node(self):
        self.current_node = tk.Label(self.canvas, text="Root", font="Arial 16 bold")
        self.current_node.pack()
        self.nodes.append(self.current_node)
        self.selected_node = self.current_node

    def open_game(self):
        file_path = filedialog.askopenfilename(filetypes=[("Text files", ".txt"), ("All files", "*.*")])
        if file_path:
            with open(file_path, "r") as f:
                data = f.read()
            self.canvas.delete("all")
            self.nodes = []
            self.current_node = None
            self.selected_node = None
            self.create_root_node()
            self.display_text(data)

    def save_game(self):
        file_path = filedialog.asksaveasfilename(filetypes=[("Text files", ".txt"), ("All files", "*.*")])
        if file_path:
            with open(file_path, "w") as f:
                f.write(self.canvas.winfo_text())

    def on_click(self, event):
        if self.selected_node:
            self.canvas.delete(self.selected_node)
            self.nodes.remove(self.selected_node)
            self.selected_node = None
        self.create_new_node()

    def create_new_node(self):
        new_node = tk.Label(self.canvas, text="New Node", font="Arial 16 bold")
        new_node.pack()
        self.nodes.append(new_node)
        self.selected_node = new_node

    def display_text(self, text):
        self.canvas.delete("all")
        self.nodes = []
        self.current_node = None
        self.selected_node = None
        self.create_root_node()
        text_label = tk.Label(self.canvas, text=text, font="Arial 12")
        text_label.pack()
        self.nodes.append(text_label)
        self.selected_node = text_label

if __name__ == "__main__":
    root = tk.Tk()
    app = TreeGameApp(root)
    root.mainloop()