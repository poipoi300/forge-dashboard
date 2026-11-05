// Service self-descriptions, fetched once and shared.
import { useQuery } from "@tanstack/react-query";
import type { JsonSchema } from "@/schema/schema";
import { listServices, type Schemas } from "./client";

export type ServiceDescriptor = Schemas["ServiceDescriptor"];
export type ActionDescriptor = Schemas["ActionDescriptor"];

export function useDescriptors(): Map<string, ServiceDescriptor> | undefined {
  const query = useQuery({
    queryKey: ["services"],
    queryFn: listServices,
    staleTime: Number.POSITIVE_INFINITY,
  });
  if (!query.data) return undefined;
  return new Map(query.data.map((descriptor) => [descriptor.id, descriptor]));
}

export function useDescriptor(service: string): ServiceDescriptor | undefined {
  return useDescriptors()?.get(service);
}

export function useActionDescriptor(service: string, action: string): ActionDescriptor | undefined {
  return useDescriptor(service)?.actions.find((item) => item.id === action);
}

/** A descriptor schema typed for the renderer (descriptors carry their own `$defs`). */
export function asSchema(schema: unknown): JsonSchema {
  return schema as JsonSchema;
}
