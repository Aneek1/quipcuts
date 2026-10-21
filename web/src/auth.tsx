// Sign-in through Clerk when VITE_CLERK_PUBLISHABLE_KEY is set; otherwise a local "dev" user,
// which the API only accepts when it runs without CLERK_ISSUER.
import { ClerkProvider, SignInButton, UserButton, useAuth } from "@clerk/clerk-react";
import { createContext, useContext, type ReactNode } from "react";

export const CLERK_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined;

type Auth = {
  ready: boolean;
  signedIn: boolean;
  getToken: () => Promise<string | null>;
  signInButton: ReactNode;
  userButton: ReactNode;
};

const devAuth: Auth = {
  ready: true,
  signedIn: true,
  getToken: async () => null,
  signInButton: null,
  userButton: <span className="devpill" title="Running without Clerk: everyone is the same local user">local dev</span>,
};

const AuthCtx = createContext<Auth>(devAuth);
export const useSession = () => useContext(AuthCtx);

function ClerkBridge({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const value: Auth = {
    ready: isLoaded,
    signedIn: !!isSignedIn,
    getToken: () => getToken(),
    signInButton: (
      <SignInButton mode="modal">
        <button className="btn sm">Sign in</button>
      </SignInButton>
    ),
    userButton: <UserButton />,
  };
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

// Clerk's sign-in window in the site's colours and type (see styles.css)
const appearance = {
  variables: {
    colorPrimary: "#15130f",
    colorText: "#15130f",
    colorBackground: "#fbf9f4",
    colorInputBackground: "#ffffff",
    fontFamily: '"Archivo", system-ui, sans-serif',
    borderRadius: "6px",
  },
};

export function AuthProvider({ children }: { children: ReactNode }) {
  if (!CLERK_KEY) return <AuthCtx.Provider value={devAuth}>{children}</AuthCtx.Provider>;
  return (
    <ClerkProvider publishableKey={CLERK_KEY} afterSignOutUrl="/" appearance={appearance}>
      <ClerkBridge>{children}</ClerkBridge>
    </ClerkProvider>
  );
}
