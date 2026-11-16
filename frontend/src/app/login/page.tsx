import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/login-form";
import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Log in — ${PRODUCT_NAME}`,
};

interface LoginPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const { registered } = await searchParams;

  return (
    <Container className="py-16 sm:py-24">
      <section aria-labelledby="login-heading" className="mx-auto max-w-md">
        <h1 id="login-heading" className="text-3xl font-semibold tracking-tight text-fg">
          Log in
        </h1>
        <div className="mt-8">
          <LoginForm registered={registered === "1"} />
        </div>
      </section>
    </Container>
  );
}
