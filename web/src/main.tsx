import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useTitle } from "./api";
import { AuthProvider, useSession } from "./auth";
import Landing from "./pages/Landing";
import { Privacy, Terms } from "./pages/Legal";
import ProjectPage from "./pages/Project";
import Editor from "./pages/Editor";
import Projects from "./pages/Projects";
import "./styles.css";

function Header() {
  const { signedIn, signInButton, userButton } = useSession();
  return (
    <header className="topbar">
      <div className="wrap">
        <Link to="/" className="brand" aria-label="Quipcuts home">
          <img src="/favicon.svg" width={26} height={26} alt="" />
          Quipcuts <span className="tag">beta</span>
        </Link>
        <nav className="nav">
          {signedIn && <NavLink to="/app">Projects</NavLink>}
          {signedIn ? userButton : signInButton}
        </nav>
      </div>
    </header>
  );
}

function RequireSignIn({ children }: { children: React.ReactNode }) {
  const { ready, signedIn, signInButton } = useSession();
  if (!ready) return <main className="wrap page"><div className="skeleton" style={{ height: 220 }} /></main>;
  if (!signedIn)
    return (
      <main className="wrap page signin">
        <h1>Sign in to upload</h1>
        <p className="muted" style={{ maxWidth: 440 }}>
          An account keeps your projects in one place and lets us hold the free beta to 3 videos a day per person.
        </p>
        {signInButton}
      </main>
    );
  return <>{children}</>;
}

function Home() {
  useTitle();
  return <Landing />;
}

function NotFound() {
  useTitle("Not found");
  return (
    <main className="wrap page">
      <span className="eyebrow">404</span>
      <h1 style={{ marginBottom: 16 }}>Nothing at this address.</h1>
      <Link to="/" className="btn">Go to the home page</Link>
    </main>
  );
}

function ScrollToTop() {
  const { pathname } = useLocation();
  useEffect(() => {
    window.scrollTo(0, 0); // Chrome now returns a Promise here, which React would treat as a cleanup
  }, [pathname]);
  return null;
}

function App() {
  return (
    <>
      <ScrollToTop />
      <Header />
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/app" element={<RequireSignIn><Projects /></RequireSignIn>} />
        <Route path="/app/p/:id" element={<RequireSignIn><ProjectPage /></RequireSignIn>} />
        <Route path="/app/c/:id" element={<RequireSignIn><Editor /></RequireSignIn>} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/terms" element={<Terms />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
      <footer className="footer">
        <div className="wrap">
          <span>Quipcuts · {new Date().getFullYear()}</span>
          <span>
            <Link to="/privacy">Privacy</Link>
            <Link to="/terms">Terms</Link>
          </span>
        </div>
      </footer>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </AuthProvider>
  </StrictMode>,
);
