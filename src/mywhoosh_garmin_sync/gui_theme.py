"""Small, dependency-free light theme for the desktop account window."""

COLORS = {
    "page": "#F4F6FA", "card": "#FFFFFF", "ink": "#152238", "muted": "#637087",
    "border": "#E1E7EF", "accent": "#087F75", "hover": "#06675F", "tint": "#E7F5F1",
    "soft": "#EDF1F6", "warning": "#975A16", "warning_bg": "#FFF4DF",
}


def apply_theme(root, ttk):
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    c = COLORS
    root.configure(background=c["page"])
    style.configure(".", font=("Segoe UI", 11), background=c["page"], foreground=c["ink"])
    style.configure("TFrame", background=c["page"])
    style.configure("Card.TFrame", background=c["card"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"], borderwidth=1, relief="solid")
    style.configure("CardBody.TFrame", background=c["card"])
    style.configure("TLabel", background=c["page"], foreground=c["ink"])
    style.configure("Brand.TLabel", foreground=c["accent"], font=("Segoe UI", 10, "bold"))
    style.configure("Title.TLabel", font=("Segoe UI", 28, "bold"))
    style.configure("Subtitle.TLabel", foreground=c["muted"], font=("Segoe UI", 11))
    style.configure("Card.TLabel", background=c["card"])
    style.configure("CardTitle.TLabel", background=c["card"], font=("Segoe UI", 17, "bold"))
    style.configure("CardMuted.TLabel", background=c["card"], foreground=c["muted"], font=("Segoe UI", 10))
    style.configure("CardStatus.TLabel", background=c["card"], foreground=c["muted"], font=("Segoe UI", 10))
    style.configure("Badge.TLabel", background=c["soft"], foreground=c["muted"],
                    font=("Segoe UI", 9, "bold"), padding=(10, 6))
    style.configure("Connected.Badge.TLabel", background=c["tint"], foreground=c["accent"])
    style.configure("Attention.Badge.TLabel", background=c["warning_bg"], foreground=c["warning"])
    style.configure("TEntry", padding=(12, 10), fieldbackground=c["card"], foreground=c["ink"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"])
    style.map("TEntry", bordercolor=[("focus", c["accent"])],
              fieldbackground=[("disabled", c["soft"])])
    style.configure("TButton", padding=(14, 10), background=c["soft"], foreground=c["ink"],
                    borderwidth=0, focuscolor=c["soft"], font=("Segoe UI", 10, "bold"))
    style.map("TButton", background=[("disabled", c["soft"]), ("active", c["border"])],
              foreground=[("disabled", "#96A0AF")])
    style.configure("Primary.TButton", background=c["accent"], foreground="white", focuscolor=c["accent"])
    style.map("Primary.TButton", background=[("disabled", c["soft"]), ("active", c["hover"])],
              foreground=[("disabled", "#96A0AF"), ("!disabled", "white")])
    style.configure("Quiet.TButton", background=c["card"], foreground=c["muted"], focuscolor=c["card"])
    style.map("Quiet.TButton", background=[("active", c["soft"])])
    style.configure("TCheckbutton", background=c["card"], font=("Segoe UI", 10),
                    indicatorbackground=c["card"], indicatorforeground=c["accent"])
    style.map("TCheckbutton", background=[("active", c["card"])],
              foreground=[("disabled", "#96A0AF")], indicatorbackground=[("selected", c["tint"])])
    style.configure("TNotebook", background=c["page"], borderwidth=0, tabmargins=(0, 6, 0, 14))
    style.configure("TNotebook.Tab", padding=(20, 12), background=c["page"], foreground=c["muted"],
                    borderwidth=0, font=("Segoe UI", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", c["card"])],
              foreground=[("selected", c["accent"])])
    style.configure("Treeview", background=c["card"], fieldbackground=c["card"], rowheight=38,
                    borderwidth=0, font=("Segoe UI", 10), foreground=c["ink"])
    style.configure("Treeview.Heading", background=c["card"], foreground=c["muted"],
                    padding=(8, 12), borderwidth=0, font=("Segoe UI", 9, "bold"))
    style.map("Treeview", background=[("selected", c["tint"])], foreground=[("selected", c["ink"])])
    style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["border"],
                    borderwidth=0, thickness=3)
    style.configure("Vertical.TScrollbar", background=c["border"], troughcolor=c["card"],
                    borderwidth=0, arrowsize=10)
    return style
