import { NextResponse, type NextRequest } from "next/server";

/**
 * Sends a browser with no session cookie to the sign-in page (Next 16's "proxy", what
 * was middleware). Only a convenience: it can't tell a valid session from an expired
 * one, and it doesn't need to. The API checks every call itself (D44), and a page that
 * gets a 401 sends you here too.
 */
export function proxy(request: NextRequest) {
  if (request.cookies.has("session")) {
    return NextResponse.next();
  }
  const signIn = request.nextUrl.clone();
  signIn.pathname = "/login";
  signIn.search = "";
  signIn.searchParams.set("next", `${request.nextUrl.pathname}${request.nextUrl.search}`);
  return NextResponse.redirect(signIn);
}

export const config = {
  // Every page but the sign-in page itself, the API (it answers 401 on its own) and
  // Next's static files.
  matcher: ["/((?!api/|login|_next/static|_next/image|favicon.ico).*)"],
};
