import type { Metadata } from "next";

import { RegisterForm } from "@/components/auth/register-form";
import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Create account — ${PRODUCT_NAME}`,
};

export default function RegisterPage() {
  return (
    <Container className="py-16 sm:py-24">
      <section aria-labelledby="register-heading" className="mx-auto max-w-md">
        <h1 id="register-heading" className="text-3xl font-semibold tracking-tight text-fg">
          Create your account
        </h1>
        <div className="mt-8">
          <RegisterForm />
        </div>
      </section>
    </Container>
  );
}
