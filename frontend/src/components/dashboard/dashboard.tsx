"use client";

import { useCallback, useEffect, useState } from "react";

import { DashboardOverview } from "@/components/dashboard/dashboard-overview";
import { ProjectPanel } from "@/components/dashboard/project-panel";
import { VideoList, type ProjectFilter } from "@/components/dashboard/video-list";
import { getErrorMessage } from "@/lib/api/client";
import { getDashboardSummary, type DashboardSummary } from "@/lib/api/dashboard";
import {
  createProject,
  listProjects,
  updateProject,
  type Project,
  type ProjectList,
} from "@/lib/api/projects";

/** Projects shown per page; they are also the choices for "move to project". */
export const DASHBOARD_PROJECT_PAGE_SIZE = 50;

/**
 * The signed-in user's dashboard: overview, projects and videos. The project
 * list is loaded once here and shared with the video list; video project
 * names come from the video responses themselves.
 */
export function Dashboard() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [summaryRequest, setSummaryRequest] = useState(0);

  const [projects, setProjects] = useState<ProjectList | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [projectsRequest, setProjectsRequest] = useState(0);
  const [projectOffset, setProjectOffset] = useState(0);

  const [projectFilter, setProjectFilter] = useState<ProjectFilter>("all");
  const [videosRequest, setVideosRequest] = useState(0);

  useEffect(() => {
    let active = true;
    getDashboardSummary().then(
      (loaded) => {
        if (active) {
          setSummary(loaded);
          setSummaryError(null);
        }
      },
      (caught: unknown) => {
        if (active) {
          setSummaryError(getErrorMessage(caught));
        }
      },
    );
    return () => {
      active = false;
    };
  }, [summaryRequest]);

  useEffect(() => {
    let active = true;
    listProjects({ limit: DASHBOARD_PROJECT_PAGE_SIZE, offset: projectOffset }).then(
      (loaded) => {
        if (active) {
          setProjects(loaded);
          setProjectsError(null);
        }
      },
      (caught: unknown) => {
        if (active) {
          setProjectsError(getErrorMessage(caught));
        }
      },
    );
    return () => {
      active = false;
    };
  }, [projectOffset, projectsRequest]);

  const reloadSummaryAndProjects = useCallback(() => {
    setSummaryRequest((count) => count + 1);
    setProjectsRequest((count) => count + 1);
  }, []);

  async function handleCreate(name: string) {
    await createProject(name);
    setProjectOffset(0);
    reloadSummaryAndProjects();
  }

  async function handleRename(project: Project, name: string) {
    await updateProject(project.id, { name });
    reloadSummaryAndProjects();
    // Listed videos carry their project's name.
    setVideosRequest((count) => count + 1);
  }

  async function handleArchive(project: Project) {
    await updateProject(project.id, { archived: true });
    if (projectFilter === project.id) {
      setProjectFilter("all");
    }
    reloadSummaryAndProjects();
    setVideosRequest((count) => count + 1);
  }

  return (
    <div className="flex flex-col gap-12">
      <h1 className="text-3xl font-semibold tracking-tight text-fg">Dashboard</h1>
      <DashboardOverview
        summary={summary}
        error={summary ? null : summaryError}
        onRetry={() => {
          setSummaryError(null);
          setSummaryRequest((count) => count + 1);
        }}
        onSelectProject={setProjectFilter}
      />
      <div className="grid gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <ProjectPanel
          projects={projects?.items ?? null}
          total={projects?.total ?? 0}
          offset={projectOffset}
          pageSize={DASHBOARD_PROJECT_PAGE_SIZE}
          error={projects ? null : projectsError}
          selectedFilter={projectFilter}
          onRetry={() => {
            setProjectsError(null);
            setProjectsRequest((count) => count + 1);
          }}
          onPage={(offset) => setProjectOffset(Math.max(0, offset))}
          onSelect={setProjectFilter}
          onCreate={handleCreate}
          onRename={handleRename}
          onArchive={handleArchive}
        />
        <VideoList
          projects={projects?.items ?? []}
          projectFilter={projectFilter}
          onProjectFilterChange={setProjectFilter}
          onVideosChanged={reloadSummaryAndProjects}
          refreshRequest={videosRequest}
        />
      </div>
    </div>
  );
}
